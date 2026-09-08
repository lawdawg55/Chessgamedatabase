# ==================== CONFIGURATION ====================
import os

CHESS_COM_USER = os.getenv("CHESS_COM_USER", "your_fallback_username")
LICHESS_USER = os.getenv("LICHESS_USER", "your_fallback_username")
EMAIL = os.getenv("CONTACT_EMAIL", "your_email@example.com")
DATABASE_URL = os.getenv("DATABASE_URL") 
# =======================================================
