import urllib.request
import json

url = "http://localhost:8080/api/v1/admin/quizzes/1v1/questions"
payload = {
    "question_text": "Which game features a battle royale mode called 'Warzone'?",
    "question_image_url": "https://images.unsplash.com/photo-1542751371-adc38448a05e?auto=format&fit=crop&q=80&w=800",
    "options": ["PUBG", "Free Fire", "Call of Duty", "Fortnite"],
    "correct_option_index": 2,
    "time_limit": 10
}

data = json.dumps(payload).encode('utf-8')

# We need the admin token. Let's try to bypass it or see if it's unprotected?
# Wait, it's protected by Depends(get_current_active_admin).
# Since I don't have the token, this will return 401 Unauthorized.
# I need to find a way to get the error from the database itself.
