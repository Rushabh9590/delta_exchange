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


def load_credentials(env_path=None, account=None):
    """
    Loads API credentials from .env or system environment.
    Supports switching between 'main' and 'sub' accounts via ACTIVE_ACCOUNT in .env.
    """
    if env_path is None:
        env_path = BASE_DIR / ".env"

    env_path = Path(env_path)
    creds = {
        "api_key": None,
        "api_secret": None,
        "base_url": URL_INDIA_PROD,
        "active_account": "main"
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
                        os.environ[k] = v
                        os.environ[k.lower()] = v
                        os.environ[k.upper()] = v
        except Exception as e:
            print(f"[WARN] Error reading .env file manually: {e}")

    # Determine requested account ('main' or 'sub')
    target_acct = (account or os.getenv("ACTIVE_ACCOUNT") or os.getenv("active_account") or "main").strip().lower()
    if target_acct not in ("main", "sub"):
        target_acct = "main"
    creds["active_account"] = target_acct

    if target_acct == "sub":
        creds["api_key"] = (
            os.getenv("SUB_API_KEY") or
            os.getenv("sub_api_key") or
            os.getenv("api_key")
        )
        creds["api_secret"] = (
            os.getenv("SUB_API_SECRET") or
            os.getenv("sub_api_secret") or
            os.getenv("api_secret")
        )
    else:
        creds["api_key"] = (
            os.getenv("MAIN_API_KEY") or
            os.getenv("main_api_key") or
            os.getenv("DELTA_API_KEY") or
            os.getenv("api_key") or
            os.getenv("API_KEY")
        )
        creds["api_secret"] = (
            os.getenv("MAIN_API_SECRET") or
            os.getenv("main_api_secret") or
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


_cached_clients = {}


def get_delta_client(api_key=None, api_secret=None, base_url=None, account=None, raise_for_status=False, force_new=False):
    """
    Initializes and returns a DeltaRestClient instance for the requested or active account.
    """
    creds = load_credentials(account=account)
    final_api_key = api_key or creds["api_key"]
    final_api_secret = api_secret or creds["api_secret"]
    final_base_url = base_url or creds["base_url"]
    active_acct = creds["active_account"]

    if not final_api_key or not final_api_secret:
        raise ValueError(
            f"Delta API Key and API Secret must be provided for account '{active_acct}' either in .env or as arguments."
        )

    cache_key = (active_acct, final_api_key, final_base_url)
    if not force_new and cache_key in _cached_clients:
        return _cached_clients[cache_key]

    client = DeltaRestClient(
        base_url=final_base_url,
        api_key=final_api_key,
        api_secret=final_api_secret,
        raise_for_status=raise_for_status
    )
    client.account_name = active_acct
    _cached_clients[cache_key] = client
    return client


def switch_account(account_name: str, update_env_file: bool = True):
    """
    Switches active account between 'main' and 'sub'.
    Optionally updates the ACTIVE_ACCOUNT parameter in .env file.
    """
    target = account_name.strip().lower()
    if target not in ("main", "sub"):
        raise ValueError(f"Invalid account '{account_name}'. Must be 'main' or 'sub'.")

    os.environ["ACTIVE_ACCOUNT"] = target
    os.environ["active_account"] = target

    if update_env_file:
        env_path = BASE_DIR / ".env"
        if env_path.exists():
            try:
                lines = []
                replaced = False
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        stripped = line.strip()
                        if stripped.startswith("ACTIVE_ACCOUNT=") or stripped.startswith("active_account="):
                            lines.append(f"ACTIVE_ACCOUNT={target}\n")
                            replaced = True
                        else:
                            lines.append(line)
                if not replaced:
                    lines.insert(0, f"ACTIVE_ACCOUNT={target}\n")
                with open(env_path, "w", encoding="utf-8") as f:
                    f.writelines(lines)
            except Exception as e:
                print(f"[WARN] Failed to write ACTIVE_ACCOUNT to .env: {e}")

    new_client = get_delta_client(account=target, force_new=True)
    global client
    client = new_client
    print(f"[ACCOUNT] Switched active account to: {target.upper()}")
    return new_client


def test_login(client=None, account=None):
    """
    Verifies authentication by making an authenticated request to Delta Exchange.
    """
    if client is None:
        client = get_delta_client(account=account)

    acct_label = getattr(client, "account_name", account or "active").upper()
    print(f"Connecting to Delta Exchange at: {client.base_url}")
    print(f"Testing credentials for Account: [{acct_label}]...")

    try:
        # Fetch wallet balances to verify authenticated session
        balances = client.get_all_wallet_balances()
        print(f"[SUCCESS] Logged in successfully as [{acct_label}]!")
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
        print(f"[ERROR] Login failed for [{acct_label}]: {e}")
        return False, str(e)


# Default client instance when imported
try:
    client = get_delta_client()
except Exception:
    client = None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Delta Exchange Authentication & Account Switcher")
    parser.add_argument("--account", choices=["main", "sub"], help="Test specific account ('main' or 'sub')")
    parser.add_argument("--switch", choices=["main", "sub"], help="Switch default account in .env to 'main' or 'sub'")
    args = parser.parse_args()

    if args.switch:
        c = switch_account(args.switch, update_env_file=True)
        test_login(c)
    else:
        try:
            c = get_delta_client(account=args.account)
            success, res = test_login(c, account=args.account)
            if not success:
                sys.exit(1)
        except Exception as ex:
            print(f"[ERROR] Client initialization failed: {ex}")
            sys.exit(1)
