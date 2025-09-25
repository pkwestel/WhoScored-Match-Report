import pandas as pd
import requests
from bs4 import BeautifulSoup
import re
from fake_useragent import UserAgent
from io import StringIO


def load_fbref_team_data(page_url):
    """
    Load team data from FBref using requests and BeautifulSoup.
    Finds tables with pattern 'stats_squads_{category}_for' and 'stats_squads_{category}_against'
    and concatenates them for each category.

    Parameters:
    page_url (str): The URL of the FBref page containing team data.

    Returns:
    dict: Dictionary with category names as keys and concatenated DataFrames as values.
    """
    try:
        # Generate a random user agent (to avoid blocking)
        ua = UserAgent()
        headers = {"User-Agent": ua.random}

        # response = requests.get(page_url) # Without user agent would get blocked

        # Send GET request to the URL
        response = requests.get(page_url, headers=headers)
        response.raise_for_status()  # Raise an exception for bad status codes

        # Parse the HTML content
        soup = BeautifulSoup(response.content, "html.parser")

        # Find all table elements with IDs matching the pattern
        table_pattern = re.compile(r"stats_squads_(.+)_(for|against)")
        tables = soup.find_all("table", id=table_pattern)

        if not tables:
            print("No matching tables found on the page")
            return {}

        # Group tables by category
        categories = {}

        for table in tables:
            table_id = table.get("id")
            match = table_pattern.match(table_id)

            if match:
                category = match.group(1)  # Extract category name
                table_type = match.group(2)  # 'for' or 'against'

                # Convert HTML table to DataFrame
                table_html = str(table)
                df = pd.read_html(StringIO(table_html))[0]

                # Initialize category dictionary if not exists
                if category not in categories:
                    categories[category] = {}

                # Store the DataFrame
                categories[category][table_type] = df

        # Concatenate 'for' and 'against' tables for each category
        combined_dataframes = {}

        for category, tables_dict in categories.items():
            for_df = tables_dict.get("for")
            against_df = tables_dict.get("against")

            if for_df is not None and against_df is not None:
                # Add a column to distinguish between 'for' and 'against' data
                for_df_copy = for_df.copy()
                against_df_copy = against_df.copy()

                for_df_copy["Table_Type"] = "For"
                against_df_copy["Table_Type"] = "Against"

                # Concatenate the DataFrames
                combined_df = pd.concat(
                    [for_df_copy, against_df_copy], ignore_index=True, sort=False
                )

                combined_dataframes[category] = combined_df

            elif for_df is not None:
                combined_dataframes[category] = for_df
                print(f"Only 'for' table found for category '{category}'")

            elif against_df is not None:
                combined_dataframes[category] = against_df
                print(f"Only 'against' table found for category '{category}'")

        return combined_dataframes

    except requests.RequestException as e:
        print(f"Error fetching data from {page_url}: {str(e)}")
        return {}
    except Exception as e:
        print(f"Error processing data: {str(e)}")
        return {}


def display_team_data_info(dataframes_dict):
    """
    Display information about the scraped team DataFrames.

    Parameters:
    dataframes_dict (dict): Dictionary of category DataFrames
    """
    if not dataframes_dict:
        print("No DataFrames to display")
        return

    print(f"\nFound {len(dataframes_dict)} categories of team data:")

    for category, df in dataframes_dict.items():
        print(f"\n--- Category: {category.upper()} ---")
        print(f"Shape: {df.shape}")
        print(f"Columns: {list(df.columns)}")
        print("First 3 rows:")
        print(df.head(3))
        print("-" * 50)


def main():
    """
    Main function to demonstrate the team data scraping functionality.
    """
    # Example FBref URL for team data
    url = "https://fbref.com/en/comps/10/Championship-Stats"

    # Scrape team data
    team_dataframes = load_fbref_team_data(url)

    # Display results
    display_team_data_info(team_dataframes)

    return team_dataframes


# Execute the main function
if __name__ == "__main__":
    scraped_team_data = main()
