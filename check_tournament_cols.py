import os, re
# get all columns in models/tournament.py
with open('models/tournament.py', 'r') as f:
    t_content = f.read()

t_cols = re.findall(r'    ([a-zA-Z_]+)\s*=\s*Column\(', t_content)
print('Tournament columns in model:', t_cols)

# get all columns added in migrations
mig_cols = []
for file in os.listdir('alembic/versions'):
    if file.endswith('.py'):
        with open('alembic/versions/' + file, 'r') as f:
            content = f.read()
            # find op.add_column('tournaments', sa.Column('colname'
            adds = re.findall(r"op\.add_column\(['\"]tournaments['\"],\s*sa\.Column\(['\"]([^'\"]+)['\"]", content)
            mig_cols.extend(adds)
            
            # find op.create_table('tournaments', sa.Column('colname'
            if "create_table('tournaments'" in content or 'create_table("tournaments"' in content:
                creates = re.findall(r"sa\.Column\(['\"]([^'\"]+)['\"]", content)
                mig_cols.extend(creates)
                
print('Missing Tournament columns:', set(t_cols) - set(mig_cols))
