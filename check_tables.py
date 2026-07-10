import os
import re

models_dir = 'models'
alembic_dir = 'alembic/versions'

tables = []
for file in os.listdir(models_dir):
    if file.endswith('.py'):
        with open(os.path.join(models_dir, file), 'r') as f:
            content = f.read()
            matches = re.findall(r'__tablename__\s*=\s*[\"\']([^\"\']+)[\"\']', content)
            tables.extend(matches)

missing_tables = []
for table in tables:
    found = False
    for file in os.listdir(alembic_dir):
        if file.endswith('.py'):
            with open(os.path.join(alembic_dir, file), 'r', encoding='utf-8') as f:
                content = f.read()
                if f"op.create_table('{table}'" in content or f'op.create_table("{table}"' in content:
                    found = True
                    break
    if not found:
        missing_tables.append(table)

print('Missing tables:', missing_tables)
