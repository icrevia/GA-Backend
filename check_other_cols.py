import os, re

def check_missing(model_file, table_name):
    with open(model_file, 'r') as f:
        content = f.read()
    
    cols = re.findall(r'    ([a-zA-Z_]+)\s*=\s*Column\(', content)
    
    mig_cols = []
    for file in os.listdir('alembic/versions'):
        if file.endswith('.py'):
            with open('alembic/versions/' + file, 'r', encoding='utf-8') as f:
                content = f.read()
                adds = re.findall(rf"op\.add_column\(['\"]{table_name}['\"],\s*sa\.Column\(['\"]([^'\"]+)['\"]", content)
                mig_cols.extend(adds)
                if f"create_table('{table_name}'" in content or f'create_table("{table_name}"' in content:
                    creates = re.findall(r"sa\.Column\(['\"]([^'\"]+)['\"]", content)
                    mig_cols.extend(creates)
                    
    missing = set(cols) - set(mig_cols)
    print(f'Missing {table_name} columns:', missing)

check_missing('models/wallet.py', 'wallet_transactions')
check_missing('models/participant.py', 'tournament_participants')
