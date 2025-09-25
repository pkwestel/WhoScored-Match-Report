from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from fake_useragent import UserAgent


class DriverContext:
    def __init__(self, track_network=False):
        self.track_network = track_network
        self.driver = None

    def __enter__(self):
        # Set up Chrome options
        options = Options()

        if self.track_network:
            # Non-headless mode for network tracking - allows visual debugging and monitoring
            options.add_argument(
                "--enable-logging"
            )  # Enable Chrome's internal logging system
            options.add_argument(
                "--log-level=0"
            )  # Set logging to most verbose level (0 = INFO, 1 = WARNING, 2 = ERROR)
            options.add_argument("--v=1")  # Enable verbose logging for debugging
            options.add_argument(
                "--enable-network-service-logging"
            )  # Enable network service logs for request monitoring
            options.add_experimental_option(
                "useAutomationExtension", False
            )  # Disable automation extension to avoid detection
            options.add_experimental_option(
                "excludeSwitches", ["enable-automation"]
            )  # Remove automation switches from Chrome

            # Enable performance logging to capture network requests and browser performance
            options.set_capability(
                "goog:loggingPrefs",
                {
                    "performance": "ALL",  # Capture all performance events including network requests
                    "browser": "ALL",  # Capture all browser console logs
                    "driver": "ALL",  # Capture all WebDriver logs
                },
            )

            # Additional options for better network monitoring and security
            options.add_argument(
                "--enable-web-security"
            )  # Keep web security enabled for realistic testing
            options.add_argument(
                "--disable-features=VizDisplayCompositor"
            )  # Disable display compositor for better performance monitoring
        else:
            # Headless mode for regular scraping - runs browser without GUI
            options.add_argument(
                "--headless=new"
            )  # Use new headless mode (more stable than old headless)

        # Common options for both modes
        options.add_argument(
            "--no-sandbox"
        )  # Bypass OS security model (required for Linux/Docker environments)
        options.add_argument(
            "--disable-dev-shm-usage"
        )  # Overcome limited resource problems in containerized environments
        options.add_argument(
            "--disable-blink-features=AutomationControlled"
        )  # Hide automation indicators from websites
        options.add_argument(
            "--disable-extensions"
        )  # Disable browser extensions for faster loading
        options.add_argument(
            "--disable-plugins"
        )  # Disable plugins like Flash for faster loading
        options.add_argument(
            "--disable-images"
        )  # Don't load images for faster page loading
        options.add_argument(
            "--disable-javascript"
        )  # Disable JavaScript execution (remove if JS is needed)

        # Add random user agent using fake-useragent library
        ua = UserAgent()
        options.add_argument(
            f"--user-agent={ua.random}"
        )  # Rotate user agent to avoid detection

        # Initialize the WebDriver using ChromeDriverManager for automatic driver management
        self.driver = webdriver.Chrome(
            service=Service(
                ChromeDriverManager().install()
            ),  # Auto-download and manage ChromeDriver
            options=options,
        )

        # Remove webdriver property to avoid detection by anti-bot systems
        # Many websites check for navigator.webdriver === true to detect automation
        self.driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        return self.driver

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.driver:
            self.driver.quit()  # Close browser and clean up resources


def get_driver(track_network=False):
    return DriverContext(track_network=track_network)


# Example usage
if __name__ == "__main__":
    print("=== Regular headless mode ===")
    with get_driver() as driver:
        driver.get("https://www.fotmob.com")
        print(f"Page title: {driver.title}")

    print("\n=== Network tracking mode ===")
    with get_driver(track_network=True) as driver:
        driver.get("https://www.sofascore.com")
        print(f"Page title: {driver.title}")

        # In network tracking mode, you can access performance logs via:
        # logs = driver.get_log('performance')
        # to monitor all network requests made by the page
