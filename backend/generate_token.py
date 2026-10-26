"""
One-time setup helper: generates a random auth token and prints the two
lines you need to paste into backend/.env and frontend/.env so they match.

Run from the backend/ directory:

    python generate_token.py
"""

from app.security import generate_token

if __name__ == "__main__":
    token = generate_token()
    print("\nGenerated a new auth token. Paste this into BOTH env files:\n")
    print(f"  backend/.env   ->  JARVIS_AUTH_TOKEN={token}")
    print(f"  frontend/.env  ->  JARVIS_AUTH_TOKEN={token}")
    print("\nThey must match exactly, or the frontend won't be able to connect.\n")
