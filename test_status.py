import requests
res = requests.get("https://web-production-051ba.up.railway.app/api/v1/status")
print(res.json())
