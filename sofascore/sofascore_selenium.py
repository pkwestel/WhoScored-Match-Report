import json
import time

from utils.driver import get_driver


def get_sofascore_data(match_url, endpoint):
    """
    Get SofaScore data by monitoring network requests for a specific endpoint.

    Parameters:
    match_url (str): The SofaScore match URL
    endpoint (str): The API endpoint to monitor (e.g., "incidents", "lineups", "statistics")

    Returns:
    dict: The JSON response data from the API call, or None if not found
    """

    print(f"Getting SofaScore data for match {match_url}, endpoint: {endpoint}")

    with get_driver(track_network=True) as driver:
        # Extract match ID from URL
        match_id = match_url.split("#id:")[-1]

        # Clear any existing logs
        driver.get_log("performance")

        # Navigate to the match page
        driver.get(match_url)

        # Scroll to bottom to ensure all data loads
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

        # Wait for page to load and make initial API calls
        time.sleep(3)

        # Look for the specific endpoint in performance logs
        target_endpoint = (
            f"/api/v1/event/{match_id}/{endpoint}"
            if endpoint
            else f"/api/v1/event/{match_id}"
        )
        print(f"Looking for API call: {target_endpoint}")

        # Get performance logs
        logs = driver.get_log("performance")

        # Search through logs for our target API call
        for log in logs:
            try:
                message = json.loads(log["message"])

                # Look for Network.responseReceived events
                if message["message"]["method"] == "Network.responseReceived":
                    response_data = message["message"]["params"]["response"]
                    url = response_data["url"]

                    # Check if this is our target API endpoint
                    if target_endpoint in url:
                        # Get HTTP method directly from response headers
                        request_method = (
                            response_data.get("requestHeaders", {})
                            .get(":method", "")
                            .upper()
                        )

                        # Skip HEAD requests
                        if request_method == "HEAD":
                            print(f"Skipping HEAD request: {url}")
                            continue

                        print(f"Found API call: {url}")
                        print(f"Status: {response_data['status']}")

                        # Get the request ID to fetch the response body
                        request_id = message["message"]["params"]["requestId"]

                        # Try to get the response body
                        try:
                            # Use Chrome DevTools Protocol to get response body
                            response_body = driver.execute_cdp_cmd(
                                "Network.getResponseBody", {"requestId": request_id}
                            )

                            if response_body and "body" in response_body:
                                # Parse the JSON response
                                json_data = json.loads(response_body["body"])
                                return json_data
                            else:
                                print("No response body found")
                                continue  # Try next log entry

                        except Exception as e:
                            print(f"Error getting response body: {str(e)}")
                            # Try alternative method - trigger the specific endpoint manually
                            continue

            except (json.JSONDecodeError, KeyError) as e:
                continue

        # If we reach here, we didn't find the endpoint in logs
        print(f"Could not find API data for endpoint: {endpoint}")
        return None


# Example usage
if __name__ == "__main__":
    match_url = "https://www.sofascore.com/football/match/juventus-borussia-dortmund/ydbsMdb#id:14566764"

    # Test different endpoints
    endpoints_to_test = ["incidents", "lineups", "average-positions"]

    for endpoint in endpoints_to_test:
        print(f"\n{'='*50}")
        print(f"Testing endpoint: {endpoint}")
        print(f"{'='*50}")

        data = get_sofascore_data(match_url, endpoint)

        if data:
            # Save the data
            with open(
                f"sofascore_{match_url.split('#id:')[-1]}_{endpoint}.json",
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(
                f"Data saved to sofascore_{match_url.split('#id:')[-1]}_{endpoint}.json"
            )
        else:
            print(f"Failed to get data for {endpoint}")
