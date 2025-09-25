import pandas as pd
import requests
from bs4 import BeautifulSoup
import re
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from io import StringIO
from fake_useragent import UserAgent
from utils.driver import get_driver


def load_fbref_player_data_with_selenium(page_url):
    """
    Load player data from FBref using requests first, then Selenium if needed.
    First tries to find table with id 'stats_{category}', if not found,
    uses Selenium to click button with id 'stats_{category}_control' to load the table.

    Parameters:
    page_url (str): The URL of the FBref page containing player data.

    Returns:
    pd.DataFrame: A DataFrame containing the player data, or None if error occurs.
    """

    # Extract category from URL (whatever comes after the number)
    category_match = re.search(r"/(\d+)/([^/]+)/", page_url)
    if not category_match:
        print("Could not extract category from URL")
        return None

    category = category_match.group(2)
    table_id = f"stats_{category}"
    button_id = f"stats_{category}_control"

    print(f"Category: {category}")
    print(f"Looking for table: {table_id}")
    print(f"Looking for button: {button_id}")

    # First try with requests and BeautifulSoup
    try:
        ua = UserAgent()
        headers = {"User-Agent": ua.random}

        print("Trying to load data with requests...")
        response = requests.get(page_url, headers=headers)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "html.parser")

        # Look for specific table with exact id
        table = soup.find("table", id=table_id)

        if table:
            print(f"Found table {table_id} with requests")
            table_html = str(table)
            df = pd.read_html(StringIO(table_html))[0]
            return df
        else:
            print(f"Table {table_id} not found with requests, trying Selenium...")

    except Exception as e:
        print(f"Error with requests approach: {str(e)}")
        print("Falling back to Selenium...")

    # If requests didn't work, try with Selenium
    try:
        with get_driver() as driver:
            print(f"Loading page with Selenium: {page_url}")
            driver.get(page_url)

            # Check if table already exists (might be loaded by default)
            try:
                table_element = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.ID, table_id))
                )
                print(f"Table {table_id} found immediately")
            except:
                print(
                    f"Table {table_id} not found, looking for control button {button_id}"
                )

                # Look for specific button
                try:
                    button = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.ID, button_id))
                    )

                    print(f"Found button {button_id}, clicking...")

                    # Scroll to the button
                    driver.execute_script("arguments[0].scrollIntoView(true);", button)
                    time.sleep(1)  # Small pause to ensure button is in view

                    # Click the button
                    try:
                        # Method 1: Regular click
                        button.click()
                    except:
                        try:
                            # Method 2: JavaScript click
                            driver.execute_script("arguments[0].click();", button)
                        except:
                            print(f"Failed to click button {button_id}")
                            return None

                    # Wait for table to appear after clicking
                    print(f"Waiting for table {table_id} to load...")
                    WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located((By.ID, table_id))
                    )

                    # Additional wait to ensure table is fully loaded
                    time.sleep(1)

                except Exception as e:
                    print(f"Error finding or clicking button {button_id}: {str(e)}")
                    return None

            # Get page source and parse with BeautifulSoup
            page_source = driver.page_source
            soup = BeautifulSoup(page_source, "html.parser")

            # Find the specific table
            table = soup.find("table", id=table_id)

            if table:
                print(f"Successfully found table {table_id} with Selenium")
                table_html = str(table)
                df = pd.read_html(StringIO(table_html))[0]
                return df
            else:
                print(f"Table {table_id} still not found after clicking button")
                return None

    except Exception as e:
        print(f"Error with Selenium approach: {str(e)}")
        return None


def main():
    """
    Main function to demonstrate the player data scraping functionality.
    """
    # FBref Liga Profesional Argentina Defense Stats URL
    url = "https://fbref.com/en/comps/21/defense/Liga-Profesional-Argentina-Stats"

    # Scrape player data
    player_df = load_fbref_player_data_with_selenium(url)

    if player_df is not None:
        print(f"\nDataFrame Shape: {player_df.shape}")
        print(f"Columns: {list(player_df.columns)}")
        print("\nFirst 5 rows:")
        print(player_df.head())
    else:
        print("Failed to load player data")

    return player_df


# Execute the main function
if __name__ == "__main__":
    scraped_player_data = main()
