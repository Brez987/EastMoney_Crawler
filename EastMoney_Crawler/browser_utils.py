import os
import sys
from pathlib import Path
from shutil import which
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.service import Service

# === 本地工具目录（项目根目录下的 tools/） ===
_SCRIPT_DIR = Path(__file__).resolve().parent          # .../EastMoney_Crawler/
_PROJECT_ROOT = _SCRIPT_DIR.parent                     # .../guba_project/
_LOCAL_TOOLS = _PROJECT_ROOT / "tools"

_LOCAL_CHROME_BINARY = str(_LOCAL_TOOLS / "chrome-win64" / "chrome.exe")
_LOCAL_CHROMEDRIVER_PATH = str(_LOCAL_TOOLS / "chromedriver-win64" / "chromedriver.exe")


def resolve_chrome_binary() -> Optional[str]:
    candidates = [
        os.environ.get("EASTMONEY_CHROME_BINARY"),
        _LOCAL_CHROME_BINARY,
        which("google-chrome"),
        which("google-chrome-stable"),
        which("chrome"),
        which("chromium"),
        which("chromium-browser"),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def resolve_chromedriver_path() -> Optional[str]:
    candidates = [
        os.environ.get("EASTMONEY_CHROMEDRIVER"),
        _LOCAL_CHROMEDRIVER_PATH,
        which("chromedriver"),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def configure_runtime_library_path():
    if sys.platform != "linux":
        return
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    existing_parts = [part for part in existing.split(":") if part]
    candidate_parts = [
        os.path.join(sys.prefix, "lib"),
        os.path.join(sys.prefix, "x86_64-conda-linux-gnu", "sysroot", "usr", "lib64"),
    ]
    merged_parts = []
    for part in candidate_parts + existing_parts:
        if part and os.path.exists(part) and part not in merged_parts:
            merged_parts.append(part)
    os.environ["LD_LIBRARY_PATH"] = ":".join(merged_parts)


def configure_local_webdriver_proxy_bypass():
    no_proxy_targets = ["127.0.0.1", "localhost"]
    for env_name in ("NO_PROXY", "no_proxy"):
        existing = os.environ.get(env_name, "")
        existing_parts = [part.strip() for part in existing.split(",") if part.strip()]
        for target in no_proxy_targets:
            if target not in existing_parts:
                existing_parts.append(target)
        os.environ[env_name] = ",".join(existing_parts)


def create_stealth_chrome(
    stealth_script_path: Optional[str] = None,
    user_data_dir: Optional[str] = None,
    incognito: bool = True,
    headless: Optional[bool] = None,
) -> webdriver.Chrome:
    configure_runtime_library_path()
    configure_local_webdriver_proxy_bypass()

    options = webdriver.ChromeOptions()
    options.add_argument("lang=zh_CN.UTF-8")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    if headless is None:
        headless = os.environ.get("EASTMONEY_CHROME_HEADLESS", "1") != "0"
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # 默认使用独立用户数据目录；财富号兜底可显式传固定 profile 复用 Cookie。
    if user_data_dir is None:
        import tempfile
        user_data_dir = tempfile.mkdtemp(prefix="chrome_")
    else:
        os.makedirs(user_data_dir, exist_ok=True)
    options.add_argument(f"--user-data-dir={user_data_dir}")
    if incognito:
        options.add_argument("--incognito")

    chrome_binary = resolve_chrome_binary()
    if chrome_binary:
        options.binary_location = chrome_binary

    chromedriver_path = resolve_chromedriver_path()
    service = Service(executable_path=chromedriver_path) if chromedriver_path else Service()
    service.env = dict(os.environ)
    browser = webdriver.Chrome(service=service, options=options)
    # 方向4：设置页面加载超时（5秒），避免慢加载页面阻塞
    browser.set_page_load_timeout(5)
    browser.set_script_timeout(3)

    if stealth_script_path is None:
        default_stealth = _SCRIPT_DIR / "stealth.min.js"
        if default_stealth.exists():
            stealth_script_path = str(default_stealth)

    if stealth_script_path and os.path.exists(stealth_script_path):
        with open(stealth_script_path, encoding="utf-8") as handle:
            js = handle.read()
        browser.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": js},
        )

    return browser
