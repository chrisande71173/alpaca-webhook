import os
import sys

# Dummy credentials, set before server.py is imported so load_dotenv() never
# puts real keys into the test process (it doesn't override existing vars).
os.environ.setdefault("APCA_API_KEY_ID", "test-key")
os.environ.setdefault("APCA_API_SECRET_KEY", "test-secret")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
