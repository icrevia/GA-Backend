import os, re
# get all columns in models/user.py
with open('models/user.py', 'r') as f:
    user_content = f.read()

user_cols = re.findall(r'    ([a-zA-Z_]+)\s*=\s*Column\(', user_content)
print('User columns in model:', user_cols)

# get all columns added in migrations
mig_cols = []
for file in os.listdir('alembic/versions'):
    if file.endswith('.py'):
        with open('alembic/versions/' + file, 'r') as f:
            content = f.read()
            # find op.add_column('users', sa.Column('colname'
            adds = re.findall(r"op\.add_column\(['\"]users['\"],\s*sa\.Column\(['\"]([^'\"]+)['\"]", content)
            mig_cols.extend(adds)
            
            # find op.create_table('users', sa.Column('colname'
            if "create_table('users'" in content or 'create_table("users"' in content:
                creates = re.findall(r"sa\.Column\(['\"]([^'\"]+)['\"]", content)
                mig_cols.extend(creates)
                
print('Missing User columns:', set(user_cols) - set(mig_cols))
