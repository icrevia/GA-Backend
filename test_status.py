import requests
res = requests.get("https://gamerzadda-backend-production.up.railway.app/api/v1/status")
print(res.json())
