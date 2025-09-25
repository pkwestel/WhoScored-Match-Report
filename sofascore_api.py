import uncurl
import requests
import json


def convert_curl_to_requests(curl_command):
    """
    Convert curl command using uncurl library and execute the request.
    Removes problematic caching headers to avoid 304 responses.

    Parameters:
    curl_command (str): The curl command as a string

    Returns:
    requests.Response: The response object from the request
    """

    try:
        # Parse the curl command
        context = uncurl.parse_context(curl_command)

        # Determine the correct HTTP method
        method = context.method
        if not any(
            x in curl_command.lower()
            for x in ["-x post", "--request post", "-x put", "-x delete", "-x patch"]
        ):
            method = "GET"  # Override method for GET requests

        print(f"Making {method} request to: {context.url}")
        print(f"Headers found: {len(context.headers) if context.headers else 0}")
        print(f"Cookies found: {len(context.cookies) if context.cookies else 0}")

        # Fix headers to avoid 304 responses
        headers = context.headers.copy() if context.headers else {}

        # Remove if-none-match header that causes 304
        headers.pop("if-none-match", None)

        # Set cache-control to no-cache
        headers["cache-control"] = "no-cache"

        print("Removed 'if-none-match' and set 'cache-control' to 'no-cache'")

        # Make the request
        response = requests.request(
            method=method,
            url=context.url,
            headers=headers,
            cookies=context.cookies,
            data=getattr(context, "data", None) if method != "GET" else None,
            timeout=30,
        )

        print(f"Response status: {response.status_code}")
        return response

    except Exception as e:
        print(f"Error converting curl command: {str(e)}")
        return None


def main():
    """
    Test the uncurl conversion with SofaScore API
    """

    curl_command = """curl 'https://www.sofascore.com/api/v1/event/14566764/average-positions' \
  -H 'accept: */*' \
  -H 'accept-language: en-US,en;q=0.9' \
  -H 'baggage: sentry-environment=production,sentry-release=YuoBx4Uo_MwzcxytPtmU4,sentry-public_key=d693747a6bb242d9bb9cf7069fb57988,sentry-trace_id=7580c9a104aa72f8c2517f66febd4476' \
  -H 'cache-control: max-age=0' \
  -b 'hb_insticator_uid=74cce853-e05e-42d4-b487-7b4d25d59cbb; _cc_id=74042f0fc206479da66caa79c15222f6; _gcl_au=1.1.19457000.1755073869; _ga=GA1.1.229150117.1755073869; panoramaId=344f026915680915bbe4a853028fa9fb927add417544909a38524182f15f3fc6; panoramaIdType=panoDevice; panoramaId_expiry=1758806490870; __gads=ID=40d273ed6ad67815:T=1755073868:RT=1758787757:S=ALNI_MYz9lDtreE6AvBj7WPTuUC23pUkQg; __gpi=UID=0000117f69436043:T=1755073868:RT=1758787757:S=ALNI_MbR0BUSDbpJzjf1lxTikqjvlJ-mCw; __eoi=ID=e0bba83983e11339:T=1755073868:RT=1758787757:S=AA-AfjZ98faKrEeFjU7GK8G3BFFQ; FCNEC=%5B%5B%22AKsRol_2WFj2LVFeDT-AXPYIrU3CSBRma_ep3_Me75Q7gKfziQYUW6VopNEEihUEIE3yRuDffX2wiZhREm2xpZIjCiZOZqPmlNE7usBSiayMvt0n0p35oaA4nsqirSaBSVA6yhsn4LbDn56rHATbN8yx_88Iw8AIOg%3D%3D%22%5D%5D; cto_bundle=89Y2lV9wZXl3QXM5dnNHWVo1Q21KdEl2RDJUY0ZWSCUyRmEzMCUyQkpIc1NqSUdncDdib1gxNTJFRmNuODZwVGhvRlBvMjVzSXp3aGFYNHZ6QXdXUjVzQ0xvTGdaRGgxQlZ0QXVsV3MyeE9DZSUyQlE2anA1RTdwNmdlSXZpVmZTOGslMkJEZkxiME1SejE2bHBaa0t3cnBESSUyRmdYaUF5WiUyRjZKdUpMRkNHZFFLYUJCbjJwMVkwTDFnZ213clVqZ2JBOEZQWkQ3WGhTUkdKd0tsMEQ4cjZGcXpVM3hkTDF0Qm9BJTNEJTNE; _ga_HNQ9P9MGZR=GS2.1.s1758787757$o5$g1$t1758787805$j12$l0$h0' \
  -H 'if-none-match: W/"16799c9a26"' \
  -H 'priority: u=1, i' \
  -H 'referer: https://www.sofascore.com/football/match/juventus-borussia-dortmund/ydbsMdb' \
  -H 'sec-ch-ua: "Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"' \
  -H 'sec-ch-ua-mobile: ?0' \
  -H 'sec-ch-ua-platform: "macOS"' \
  -H 'sec-fetch-dest: empty' \
  -H 'sec-fetch-mode: cors' \
  -H 'sec-fetch-site: same-origin' \
  -H 'sentry-trace: 7580c9a104aa72f8c2517f66febd4476-ad80e25e60d3cd19' \
  -H 'user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36' \
  -H 'x-requested-with: cc4d70'"""

    # Convert and execute the curl command
    response = convert_curl_to_requests(curl_command)

    if response:
        if response.status_code == 200:
            try:
                # Parse JSON response
                json_data = response.json()

                # Preview the data
                print(f"\nData preview:")
                print(json.dumps(json_data, indent=2)[:1000])  # Print first 1000 chars

            except json.JSONDecodeError:
                print("Response is not valid JSON")
                print("Text response:", response.text[:500])
        else:
            print(f"Request failed with status {response.status_code}")
            print("Response:", response.text[:500])
    else:
        print("Failed to make request")


if __name__ == "__main__":
    main()
