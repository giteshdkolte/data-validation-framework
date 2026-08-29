from dotenv import load_dotenv
import os

def load_env_as_dict(env_path: str = ".env") -> dict:
    """
    Load environment variables from a .env file and return them as a dictionary.

    Args:
        env_path: Path to the .env file (default: ".env" in current directory)

    Returns:
        Dictionary of key-value pairs loaded from the .env file
    """
    load_dotenv(dotenv_path=env_path)

    env_dict = {}
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                env_dict[key] = os.getenv(key, value)

    return env_dict