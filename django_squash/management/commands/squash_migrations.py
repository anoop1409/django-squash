import ast
import inspect
import itertools
import os
import re
import sys
import types
from collections import defaultdict

from django import get_version
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import migrations as migration_module
from django.db.migrations.autodetector import MigrationAutodetector as MigrationAutodetectorBase
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.questioner import NonInteractiveMigrationQuestioner as NonInteractiveMigrationQuestionerBase
from django.db.migrations.state import ProjectState
from django.db.migrations.writer import (
    MIGRATION_HEADER_TEMPLATE, MIGRATION_TEMPLATE, MigrationWriter as MigrationWriterBase, OperationWriter,
)
from django.utils.timezone import now


class Migration(migration_module.Migration):

    def __getitem__(self, index):
        return (self.app_label, self.name)[index]

    def __iter__(self):
        yield from (self.app_label, self.name)

    @classmethod
    def from_migration(cls, migration):
        new = Migration(name=migration.name, app_label=migration.app_label)
        new.__dict__ = migration.__dict__.copy()
        return new


def all_custom_operations(operations):
    """
    Generator that loops over all the operations and traverses sub-operations such as those inside a -
    SeparateDatabaseAndState class.
    """
    for operation in operations:
        if operation.elidable:
            continue

        if isinstance(operation, migration_module.RunSQL) or isinstance(operation, migration_module.RunPython):
            yield operation
        elif isinstance(operation, migration_module.SeparateDatabaseAndState):
            yield from all_custom_operations(operation.state_operations)
            # Just in case we added something in here incorrectly
            # This should always return nothing since it should NEVER have any RunSQL / RunPython
            yield from all_custom_operations(operation.database_operations)


def get_imports(module):
    """
    Return an generator with all the imports to a particular py file as string
    """
    source = inspect.getsource(module)
    path = inspect.getsourcefile(module)

    root = ast.parse(source, path)
    for node in ast.iter_child_nodes(root):
        if isinstance(node, ast.Import):
            for n in node.names:
                yield f'import {n.name}'
        elif isinstance(node, ast.ImportFrom):
            module = node.module.split('.')
            # Remove old python 2.x imports
            if '__future__' not in node.module:
                yield f"from {node.module} import {', '.join([x.name for x in node.names])}"
        else:
            continue


def copy_func(f, name=None):
    return types.FunctionType(f.__code__, f.__globals__, name or f.__name__,
                              f.__defaults__, f.__closure__)


class ReplacementMigrationWriter(MigrationWriterBase):
    """
    Take a Migration instance and is able to produce the contents
    of the migration file from it.
    """
    template_class_header = MIGRATION_HEADER_TEMPLATE
    template_class = MIGRATION_TEMPLATE

    def __init__(self, migration, include_header=True):
        self.migration = migration
        self.include_header = include_header
        self.needs_manual_porting = False

    def as_string(self):
        """Return a string of the file contents."""
        return self.template_class % self.get_kwargs()

    def get_kwargs(self):
        items = {
            "replaces_str": "",
            "initial_str": "",
        }

        imports = set()

        # Deconstruct operations
        operations = []
        for operation in self.migration.operations:
            operation_string, operation_imports = OperationWriter(operation).serialize()
            imports.update(operation_imports)
            operations.append(operation_string)
        items["operations"] = "\n".join(operations) + "\n" if operations else ""

        # Format dependencies and write out swappable dependencies right
        dependencies = []
        for dependency in self.migration.dependencies:
            if dependency[0] == "__setting__":
                dependencies.append("        migrations.swappable_dependency(settings.%s)," % dependency[1])
                imports.add("from django.conf import settings")
            else:
                dependencies.append("        %s," % self.serialize(dependency)[0])
        items["dependencies"] = "\n".join(dependencies) + "\n" if dependencies else ""

        # Format imports nicely, swapping imports of functions from migration files
        # for comments
        migration_imports = set()
        for line in list(imports):
            if re.match(r"^import (.*)\.\d+[^\s]*$", line):
                migration_imports.add(line.split("import")[1].strip())
                imports.remove(line)
                self.needs_manual_porting = True

        # django.db.migrations is always used, but models import may not be.
        # If models import exists, merge it with migrations import.
        if "from django.db import models" in imports:
            imports.discard("from django.db import models")
            imports.add("from django.db import migrations, models")
        else:
            imports.add("from django.db import migrations")

        # Sort imports by the package / module to be imported (the part after
        # "from" in "from ... import ..." or after "import" in "import ...").
        sorted_imports = sorted(imports, key=lambda i: i.split()[1])
        items["imports"] = "\n".join(sorted_imports) + "\n" if imports else ""
        if migration_imports:
            items["imports"] += (
                "\n\n# Functions from the following migrations need manual "
                "copying.\n# Move them and any dependencies into this file, "
                "then update the\n# RunPython operations to refer to the local "
                "versions:\n# %s"
            ) % "\n# ".join(sorted(migration_imports))
        # If there's a replaces, make a string for it
        if self.migration.replaces:
            items['replaces_str'] = "\n    replaces = %s\n" % self.serialize(self.migration.replaces)[0]
        # Hinting that goes into comment
        if self.include_header:
            items['migration_header'] = self.template_class_header % {
                'version': get_version(),
                'timestamp': now().strftime("%Y-%m-%d %H:%M"),
            }
        else:
            items['migration_header'] = ""

        if self.migration.initial:
            items['initial_str'] = "\n    initial = True\n"

        return items


class MigrationWriter(ReplacementMigrationWriter):
    template_class = """\
%(migration_header)s%(imports)s%(functions)s

class Migration(migrations.Migration):
%(replaces_str)s%(initial_str)s
    dependencies = [
%(dependencies)s\
    ]

    operations = [
%(operations)s\
    ]
"""

    def get_kwargs(self):
        kwargs = super().get_kwargs()

        functions = []

        for operation in self.migration.operations:
            if isinstance(operation, migration_module.RunPython):
                functions.append(inspect.getsource(operation.code))

        kwargs['operations'] = kwargs['operations'].replace('DELETEMEPLEASE.', '')
        kwargs['imports'] = kwargs['imports'].replace('import DELETEMEPLEASE\n', '')
        kwargs['functions'] = ('\n\n' if functions else '') + '\n\n'.join(functions)

        return kwargs


class SquashMigrationAutodetector(MigrationAutodetectorBase):

    def add_non_elidables(self, loader, changes):
        replacing_migrations_by_app = {app: [loader.disk_migrations[r]
                                             for r in itertools.chain.from_iterable([m.replaces for m in migrations])]
                                       for app, migrations in changes.items()}

        for app in changes.keys():
            operations = []
            imports = []

            for migration in replacing_migrations_by_app[app]:
                module = sys.modules[migration.__module__]
                imports.extend(get_imports(module))
                for operation in all_custom_operations(migration.operations):
                    operation.code = copy_func(operation.code)
                    # TODO: get a better name?
                    operation.code.__module__ = 'DELETEMEPLEASE'
                    operations.append(operation)

            migration = changes[app][-1]
            migration.operations += operations
            migration.extra_imports = imports

    def replace_current_migrations(self, graph, changes):
        """
        Adds 'replaces' to the squash migrations with all the current apps we have.
        """
        migrations_by_app = defaultdict(list)
        for app, migration in graph.node_map:
            migrations_by_app[app].append((app, migration))

        for app, migrations in changes.items():
            for migration in migrations:
                # TODO: maybe use use a proper order???
                migration.replaces = sorted(migrations_by_app[app])

    def rename_migrations(self, graph, changes, migration_name=None):
        """
        Continues the numbering from whats there now.
        """
        current_counters_by_app = defaultdict(int)
        for app, migration in graph.node_map:
            current_counters_by_app[app] = max([int(migration[:4]), current_counters_by_app[app]])

        for app, migrations in changes.items():
            for migration in migrations:
                next_number = current_counters_by_app[app] + 1
                migration.name = "%04i_%s" % (
                    next_number,
                    migration_name or 'squashed',
                )

    def _detect_changes(self, convert_apps=None, graph=None):
        """
        Swap django.db.migrations.Migration with a custom one that behaves like a tuple.
        """
        super()._detect_changes(convert_apps=convert_apps, graph=graph)

        # First pass, swapping the objects
        migrations_by_name = {}
        for key in self.migrations:
            new_migrations = []
            for migration in self.migrations[key]:
                new_migration = Migration.from_migration(migration)
                new_migrations.append(new_migration)
                migrations_by_name.setdefault(tuple(new_migration), new_migration)
            self.migrations[key] = new_migrations

        # Second pass, replace the tuples with the newly created objects
        for migration in migrations_by_name.values():
            new_dependencies = []
            for dependency in migration.dependencies:
                new_dependencies.append(migrations_by_name[dependency])
            migration.dependencies = new_dependencies

        return self.migrations

    def squash(self, loader, trim_to_apps=None, convert_apps=None, migration_name=None):
        new_graph = MigrationGraph()  # Don't care what the tree is, we want a blank slate
        changes = super().changes(new_graph, trim_to_apps, convert_apps, migration_name)

        graph = loader.graph

        self.rename_migrations(graph, changes, migration_name)
        self.replace_current_migrations(graph, changes)
        self.add_non_elidables(loader, changes)

        return changes


class NonInteractiveMigrationQuestioner(NonInteractiveMigrationQuestionerBase):
    def ask_initial(self, *args, **kwargs):
        # Ensures that the 0001_initial will always be generated
        return True


class Command(BaseCommand):

    def add_arguments(self, parser):
        parser.add_argument(
            'args', metavar='app_label', nargs='*',
            help='Specify the app label(s) to create migrations for.',
        )

    def handle(self, *app_labels, **kwargs):
        self.verbosity = 1
        self.include_header = False
        self.dry_run = False

        # Make sure the app they asked for exists
        app_labels = set(app_labels)
        has_bad_labels = False
        for app_label in app_labels:
            try:
                apps.get_app_config(app_label)
            except LookupError as err:
                self.stderr.write(str(err))
                has_bad_labels = True
        if has_bad_labels:
            sys.exit(2)

        self.migration_name = ''

        loader = MigrationLoader(None, ignore_no_migrations=True)

        questioner = NonInteractiveMigrationQuestioner(specified_apps=app_labels, dry_run=False)
        # Set up autodetector
        autodetector = SquashMigrationAutodetector(
            ProjectState(),
            ProjectState.from_apps(apps),
            questioner,
        )

        changes = autodetector.squash(
            loader=loader,
            trim_to_apps=app_labels or None,
            convert_apps=app_labels or None,
            migration_name=self.migration_name,
        )

        replacing_migrations = 0
        for migration in itertools.chain.from_iterable(changes.values()):
            replacing_migrations += len(migration.replaces)

        if not replacing_migrations:
            raise CommandError("There are no migrations to squash.")

        self.write_migration_files(changes)

    def write_migration_files(self, changes):
        """
        Take a changes dict and write them out as migration files.
        """
        directory_created = {}
        for app_label, app_migrations in changes.items():
            if self.verbosity >= 1:
                self.stdout.write(self.style.MIGRATE_HEADING("Migrations for '%s':" % app_label) + "\n")
            for migration in app_migrations:
                # Describe the migration
                writer = MigrationWriter(migration, self.include_header)
                if self.verbosity >= 1:
                    # Display a relative path if it's below the current working
                    # directory, or an absolute path otherwise.
                    try:
                        migration_string = os.path.relpath(writer.path)
                    except ValueError:
                        migration_string = writer.path
                    if migration_string.startswith('..'):
                        migration_string = writer.path
                    self.stdout.write("  %s\n" % (self.style.MIGRATE_LABEL(migration_string),))
                    for operation in migration.operations:
                        self.stdout.write("    - %s\n" % operation.describe())
                if not self.dry_run:
                    # Write the migrations file to the disk.
                    migrations_directory = os.path.dirname(writer.path)
                    if not directory_created.get(app_label):
                        os.makedirs(migrations_directory, exist_ok=True)
                        init_path = os.path.join(migrations_directory, "__init__.py")
                        if not os.path.isfile(init_path):
                            open(init_path, "w").close()
                        # We just do this once per app
                        directory_created[app_label] = True
                    migration_string = writer.as_string()
                    with open(writer.path, "w", encoding='utf-8') as fh:
                        fh.write(migration_string)
                elif self.verbosity == 3:
                    # Alternatively, makemigrations --dry-run --verbosity 3
                    # will output the migrations to stdout rather than saving
                    # the file to the disk.
                    self.stdout.write(self.style.MIGRATE_HEADING(
                        "Full migrations file '%s':" % writer.filename) + "\n"
                    )
                    self.stdout.write("%s\n" % writer.as_string())
