import os, re

def generate_add_columns(model_file, table_name, missing_cols):
    with open(model_file, 'r') as f:
        content = f.read()
    
    print(f"\n# {table_name}")
    for col in missing_cols:
        # find the line where this column is defined
        match = re.search(fr'    {col}\s*=\s*Column\((.+?)\)\s*(?:#.*)?$', content, re.MULTILINE)
        if match:
            col_def = match.group(1).strip()
            # col_def contains everything inside Column(...)
            # We need to construct sa.Column('{col}', {col_def})
            # Convert SQLAlchemy imports to sa.*
            col_def = re.sub(r'([A-Z][a-zA-Z]+)', r'sa.\1', col_def)
            # handle func.now()
            col_def = col_def.replace('func.', 'sa.func.')
            print(f'        batch_op.add_column(sa.Column("{col}", {col_def}))')
        else:
            print(f'        # COULD NOT FIND DEFINITION FOR {col}')

user_missing = {'winning_balance', 'password_hash', 'phone_number', 'last_login_device', 'referral_code', 'xp', 'bonus_balance', 'daily_bonus_cycle_key', 'profile_pic', 'daily_bonus_used', 'mmr', 'level', 'deposit_balance', 'last_login_at', 'fcm_token', 'daily_spin_used', 'token_version', 'admin_permissions', 'daily_spin_cycle_key', 'daily_spin_limit', 'referred_by_id', 'last_login_ip'}
tournament_missing = {'prize_distribution', 'max_slots', 'map_name', 'per_kill_prize', 'match_type'}
wallet_missing = {'payment_mode', 'gateway_payment_id', 'gateway_signature', 'gateway_order_id', 'payu_txn_id', 'failure_reason', 'remark'}
tp_missing = {'game_uid', 'game_username', 'account_level', 'slot_no', 'team_members_raw'} # Excluded team_name, join_code, is_captain as they were in a1b2c3d4e5f6

generate_add_columns('models/user.py', 'users', user_missing)
generate_add_columns('models/tournament.py', 'tournaments', tournament_missing)
generate_add_columns('models/wallet.py', 'wallet_transactions', wallet_missing)
generate_add_columns('models/participant.py', 'tournament_participants', tp_missing)

