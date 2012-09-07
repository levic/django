from django.db.backends.schema import BaseDatabaseSchemaEditor
from django.db.models.loading import cache
from django.db.models.fields.related import ManyToManyField


class DatabaseSchemaEditor(BaseDatabaseSchemaEditor):

    sql_delete_table = "DROP TABLE %(table)s"

    def _remake_table(self, model, create_fields=[], delete_fields=[], alter_fields=[], rename_fields=[], override_uniques=None):
        "Shortcut to transform a model from old_model into new_model"
        # Work out the new fields dict / mapping
        body = dict((f.name, f) for f in model._meta.local_fields)
        mapping = dict((f.column, f.column) for f in model._meta.local_fields)
        # If any of the new or altered fields is introducing a new PK,
        # remove the old one
        restore_pk_field = None
        if any(f.primary_key for f in create_fields) or any(n.primary_key for o, n in alter_fields):
            for name, field in list(body.items()):
                if field.primary_key:
                    field.primary_key = False
                    restore_pk_field = field
                    if field.auto_created:
                        del body[name]
                        del mapping[field.column]
        # Add in any created fields
        for field in create_fields:
            body[field.name] = field
        # Add in any altered fields
        for (old_field, new_field) in alter_fields:
            del body[old_field.name]
            del mapping[old_field.column]
            body[new_field.name] = new_field
            mapping[new_field.column] = old_field.column
        # Remove any deleted fields
        for field in delete_fields:
            del body[field.name]
            del mapping[field.column]
        # Construct a new model for the new state
        meta_contents = {
            'app_label': model._meta.app_label,
            'db_table': model._meta.db_table + "__new",
            'unique_together': model._meta.unique_together if override_uniques is None else override_uniques,
        }
        meta = type("Meta", tuple(), meta_contents)
        body['Meta'] = meta
        body['__module__'] = "__fake__"
        with cache.temporary_state():
            del cache.app_models[model._meta.app_label][model._meta.object_name.lower()]
            temp_model = type(model._meta.object_name, model.__bases__, body)
        # Create a new table with that format
        self.create_model(temp_model)
        # Copy data from the old table
        field_maps = list(mapping.items())
        self.execute("INSERT INTO %s (%s) SELECT %s FROM %s;" % (
            self.quote_name(temp_model._meta.db_table),
            ', '.join([x for x, y in field_maps]),
            ', '.join([y for x, y in field_maps]),
            self.quote_name(model._meta.db_table),
        ))
        # Delete the old table
        self.delete_model(model)
        # Rename the new to the old
        self.alter_db_table(model, temp_model._meta.db_table, model._meta.db_table)
        # Run deferred SQL on correct table
        for sql in self.deferred_sql:
            self.execute(sql.replace(temp_model._meta.db_table, model._meta.db_table))
        self.deferred_sql = []
        # Fix any PK-removed field
        if restore_pk_field:
            restore_pk_field.primary_key = True

    def create_field(self, model, field):
        """
        Creates a field on a model.
        Usually involves adding a column, but may involve adding a
        table instead (for M2M fields)
        """
        # Special-case implicit M2M tables
        if isinstance(field, ManyToManyField) and field.rel.through._meta.auto_created:
            return self.create_model(field.rel.through)
        # Detect bad field combinations
        if (not field.null and
           (not field.has_default() or field.get_default() is None) and
           not field.empty_strings_allowed):
            raise ValueError("You cannot add a null=False column without a default value on SQLite.")
        self._remake_table(model, create_fields=[field])

    def delete_field(self, model, field):
        """
        Removes a field from a model. Usually involves deleting a column,
        but for M2Ms may involve deleting a table.
        """
        # Special-case implicit M2M tables
        if isinstance(field, ManyToManyField) and field.rel.through._meta.auto_created:
            return self.delete_model(field.rel.through)
        # For everything else, remake.
        self._remake_table(model, delete_fields=[field])

    def alter_field(self, model, old_field, new_field, strict=False):
        # Ensure this field is even column-based
        old_type = old_field.db_type(connection=self.connection)
        new_type = self._type_for_alter(new_field)
        if old_type is None and new_type is None:
            # TODO: Handle M2M fields being repointed
            return
        elif old_type is None or new_type is None:
            raise ValueError("Cannot alter field %s into %s - they are not compatible types" % (
                    old_field,
                    new_field,
                ))
        # Alter by remaking table
        self._remake_table(model, alter_fields=[(old_field, new_field)])

    def alter_unique_together(self, model, old_unique_together, new_unique_together):
        self._remake_table(model, override_uniques=new_unique_together)
