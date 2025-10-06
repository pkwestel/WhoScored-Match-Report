import requests
from bs4 import BeautifulSoup
import re
from fake_useragent import UserAgent


def get_transfermarkt_player_info(player_url):
    """
    Get player information from Transfermarkt player profile page.

    Parameters:
    player_url (str): The Transfermarkt player URL

    Returns:
    dict: Dictionary containing player information
    """
    try:
        response = requests.get(player_url, headers={"User-Agent": UserAgent().random})
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "html.parser")

        player_info = {}

        # Get player name and shirt number
        headline_container = soup.find("div", class_="data-header__headline-container")
        if headline_container:
            # Get shirt number
            shirt_number_span = headline_container.find(
                "span", class_="data-header__shirt-number"
            )
            if shirt_number_span:
                shirt_text = shirt_number_span.get_text(strip=True)
                player_info["shirt_number"] = shirt_text.replace("#", "").strip()

            # Get player name
            headline_wrapper = headline_container.find(
                "h1", class_="data-header__headline-wrapper"
            )
            if headline_wrapper:
                # Remove shirt number span and get remaining text
                shirt_span = headline_wrapper.find(
                    "span", class_="data-header__shirt-number"
                )
                if shirt_span:
                    shirt_span.extract()

                player_name = headline_wrapper.get_text(strip=True)
                player_info["player_name"] = player_name

        # Get market value
        market_value_wrapper = soup.find(
            "a", class_="data-header__market-value-wrapper"
        )
        if market_value_wrapper:
            market_value_text = market_value_wrapper.get_text(strip=True)
            # Clean up the text and extract value with currency
            market_value_clean = re.sub(
                r"Last update:.*", "", market_value_text
            ).strip()
            player_info["market_value"] = market_value_clean

        # Get detailed player information from info table
        info_table = soup.find("div", class_="info-table info-table--right-space")
        if info_table:
            # Get all content spans
            content_spans = info_table.find_all("span", class_="info-table__content")

            for i in range(0, len(content_spans), 2):
                if i + 1 < len(content_spans):
                    label_span = content_spans[i]
                    value_span = content_spans[i + 1]

                    label = label_span.get_text(strip=True).replace(":", "")
                    value = value_span.get_text(strip=True)

                    # Map labels to our desired keys
                    if "Name in home country" in label:
                        player_info["full_name"] = value
                    elif "Date of birth" in label:
                        # Extract date and age separately
                        if "(" in value and ")" in value:
                            date_part = value.split("(")[0].strip()
                            age_part = value.split("(")[1].split(")")[0].strip()
                            player_info["date_of_birth"] = date_part
                            player_info["age"] = age_part
                        else:
                            player_info["date_of_birth"] = value
                    elif "Place of birth" in label:
                        player_info["place_of_birth"] = value
                    elif "Height" in label:
                        player_info["height"] = value
                    elif "Citizenship" in label:
                        player_info["citizenship"] = value
                    elif "Position" in label:
                        player_info["position"] = value
                    elif "Foot" in label:
                        player_info["foot"] = value
                    elif "Player agent" in label:
                        # Get agent name from link if available
                        agent_link = value_span.find("a")
                        if agent_link:
                            player_info["player_agent"] = agent_link.get_text(
                                strip=True
                            )
                        else:
                            player_info["player_agent"] = value
                    elif "Current club" in label:
                        # Get club name from link if available
                        club_link = value_span.find("a")
                        if club_link:
                            player_info["current_club"] = club_link.get_text(strip=True)
                        else:
                            player_info["current_club"] = value
                    elif "Joined" in label:
                        player_info["joining_date"] = value
                    elif "Contract expires" in label:
                        player_info["contract_expiry"] = value
                    elif "Contract option" in label:
                        player_info["contract_option"] = value

        return player_info

    except requests.RequestException as e:
        print(f"Error fetching player data: {e}")
        return None
    except Exception as e:
        print(f"Error parsing player data: {e}")
        return None


# Example usage
if __name__ == "__main__":
    player_url = "https://www.transfermarkt.com/bryan-mbeumo/profil/spieler/413039"

    player_data = get_transfermarkt_player_info(player_url)

    if player_data:
        print("Player Information:")
        print("=" * 50)
        for key, value in player_data.items():
            print(f"{key.replace('_', ' ').title()}: {value}")
    else:
        print("Failed to retrieve player information")

"""
Additional Endpoints:
https://www.transfermarkt.com/ceapi/LatestTransfers/list/all: Fetch all latest transfers
https://www.transfermarkt.com/quickselect/teams/GB1: Fetch all teams by league ID
https://www.transfermarkt.com/quickselect/players/985: Fetch all players by team ID
https://tmapi-alpha.transfermarkt.technology/player/503883/national-career-history: Fetch player's national career history
https://tmapi-alpha.transfermarkt.technology/player/503883/gallery: Fetch player's image gallery
"""
