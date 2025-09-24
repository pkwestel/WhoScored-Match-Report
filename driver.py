from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from fake_useragent import UserAgent


class DriverContext:
    def __enter__(self):
        # Set up Chrome options
        options = Options()
        options.add_argument("--headless=new")  # Run browser without GUI
        options.add_argument(
            "--no-sandbox"
        )  # Bypass OS security model (required for Linux/Docker)
        options.add_argument(
            "--disable-dev-shm-usage"
        )  # Overcome limited resource problems
        options.add_argument(
            "--disable-blink-features=AutomationControlled"
        )  # Hide automation indicators

        # Add random user agent using fake-useragent
        ua = UserAgent()
        options.add_argument(
            f"--user-agent={ua.random}"
        )  # Rotate user agent to avoid detection

        # Initialize the WebDriver using the ChromeDriverManager
        self.driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()), options=options
        )

        # Remove webdriver property to avoid detection by anti-bot systems
        # Many websites check for navigator.webdriver === true to detect automation
        self.driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        return self.driver

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.driver.quit()


def get_driver():
    return DriverContext()


# Example usage
if __name__ == "__main__":
    with get_driver() as driver:
        driver.get("https://www.google.com")
        print(f"Page title: {driver.title}")
        print("Driver will automatically close when exiting the context")
