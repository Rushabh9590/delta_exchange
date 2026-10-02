import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Ensure local python-rest-client package directory is accessible in sys.path
BASE_DIR = Path(__file__).resolve().parent
REPO_PATH = BASE_DIR / "python-rest-client"
if str(REPO_PATH) not in sys.path:
    sys.path.insert(0, str(REPO_PATH))

from delta_rest_client import DeltaRestClient, OrderType, TimeInForce

# Delta Exchange Base URLs
URL_INDIA_PROD = "https://api.india.delta.exchange"
URL_INDIA_TESTNET = "https://cdn-ind.testnet.deltaex.org"
URL_GLOBAL_PROD = "https://api.delta.exchange"
URL_GLOBAL_TESTNET = "https://testnet-api.delta.exchange"


def load_credentials(env_path=None):
    """
    Loads API credentials from .env or system environment.
    Supports standard KEY=VALUE as well as KEY:VALUE syntax.
    """
    if env_path is None:
        env_path = BASE_DIR / ".env"

    env_path = Path(env_path)
    creds = {
        "api_key": None,
        "api_secret": None,
        "base_url": URL_INDIA_PROD
    }

    if env_path.exists():
        # Load dotenv quietly
        load_dotenv(dotenv_path=env_path, override=True)

        # Fallback manual parser to support any custom formatting
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                    elif ":" in line:
                        k, v = line.split(":", 1)
                    else:
                        continue
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and v:
                        os.environ.setdefault(k, v)
                        os.environ.setdefault(k.lower(), v)
                        os.environ.setdefault(k.upper(), v)
        except Exception as e:
            print(f"[WARN] Error reading .env file manually: {e}")

    # Read from environment
    creds["api_key"] = (
        os.getenv("DELTA_API_KEY") or
        os.getenv("api_key") or
        os.getenv("API_KEY")
    )
    creds["api_secret"] = (
        os.getenv("DELTA_API_SECRET") or
        os.getenv("api_secret") or
        os.getenv("API_SECRET")
    )
    creds["base_url"] = (
        os.getenv("DELTA_BASE_URL") or
        os.getenv("base_url") or
        os.getenv("BASE_URL") or
        URL_INDIA_PROD
    )

    return creds


def get_delta_client(api_key=None, api_secret=None, base_url=None, raise_for_status=False):
    """
    Initializes and returns a DeltaRestClient instance.
    """
    creds = load_credentials()
    final_api_key = api_key or creds["api_key"]
    final_api_secret = api_secret or creds["api_secret"]
    final_base_url = base_url or creds["base_url"]

    if not final_api_key or not final_api_secret:
        raise ValueError(
            "Delta API Key and API Secret must be provided either in .env or as arguments."
        )

    client = DeltaRestClient(
        base_url=final_base_url,
        api_key=final_api_key,
        api_secret=final_api_secret,
        raise_for_status=raise_for_status
    )
    return client


def test_login(client=None):
    """
    Verifies authentication by making an authenticated request to Delta Exchange.
    """
    if client is None:
        client = get_delta_client()

    print(f"Connecting to Delta Exchange at: {client.base_url}")
    print("Testing credentials...")

    try:
        # Fetch wallet balances to verify authenticated session
        balances = client.get_all_wallet_balances()
        print("[SUCCESS] Logged in successfully!")
        print("Account Wallet Balances:")
        if balances:
            for b in balances:
                asset_id = b.get("asset_id")
                balance = b.get("balance", "0")
                available = b.get("available_balance", "0")
                asset_symbol = b.get("asset_symbol") or (b.get("asset", {}).get("symbol") if isinstance(b.get("asset"), dict) else f"Asset #{asset_id}")
                print(f"  - {asset_symbol}: Balance = {balance}, Available = {available}")
        else:
            print("  - No active wallet balances found (Balance is 0).")
        return True, balances
    except Exception as e:
        print(f"[ERROR] Login failed: {e}")
        return False, str(e)


# Default client instance when imported
try:
    client = get_delta_client()
except Exception:
    client = None


if __name__ == "__main__":
    try:
        c = get_delta_client()
        success, res = test_login(c)
        if not success:
            sys.exit(1)
    except Exception as ex:
        print(f"[ERROR] Client initialization failed: {ex}")
        sys.exit(1)
