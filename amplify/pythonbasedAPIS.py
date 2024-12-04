import time
import pandas as pd
import json
import requests
import schedule
import threading
from flask import Flask, jsonify, request
from flask_cors import CORS
import logging
logging.basicConfig(level=logging.INFO)

# Logging configuration
logging.basicConfig(level=logging.INFO)


# Initialize Flask app
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})  # Allow all origins for debugging

# URLs for Nifty and Bank Nifty Option Chain API
nifty_url = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
banknifty_url = "https://www.nseindia.com/api/option-chain-indices?symbol=BANKNIFTY"

# Headers and cookies
headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Referer": "https://www.nseindia.com/option-chain"
}
cookies = {}

# Global storage for intraday data
intraday_data = []
nifty_intraday_data = []
banknifty_intraday_data = []

def fetch_data(url, symbol):
    """Fetch data from the NSE API."""
    try:
        with requests.Session() as session:
            session.headers.update(headers)
            session.cookies.update(cookies)
            response = session.get(url)
            if response.status_code == 200:
                print(f"Data fetched successfully for {symbol}")
                return response.json()
            else:
                print(f"Failed to fetch data for {symbol}, Status Code: {response.status_code}")
                return None
    except Exception as e:
        print(f"Error fetching data for {symbol}: {e}")
        return None
    
   

def calculate_odin_percentage(change_in_oi, open_interest):
    """Calculate Odin Percentage."""
    if open_interest > 0:
        return round((change_in_oi / open_interest) * 100, 2)
    return 0

def filter_call_put_data(option_chain, underlying_value, range_points=None):
    """Filter call and put data based on strike price range."""
    if underlying_value is None:
        print("Error: Underlying value is None.")
        return [], []  # Return empty lists if underlying value is missing.

    min_strike, max_strike = (
        (underlying_value - range_points, underlying_value + range_points)
        if range_points is not None
        else (float('-inf'), float('inf'))  # Include all strikes if no range provided
    )

    call_data, put_data = [], []

    for item in option_chain:
        ce_data, pe_data = item.get("CE", {}), item.get("PE", {})
        strike_price = ce_data.get("strikePrice", pe_data.get("strikePrice", 0))

        if not (min_strike <= strike_price <= max_strike):
            continue

        if ce_data:
            call_data.append({
                "strikePrice": strike_price,
                "lastPrice": ce_data.get("lastPrice", "N/A"),
                "openInterest": ce_data.get("openInterest", 0),
                "changeInOI": ce_data.get("changeinOpenInterest", 0),
                "volume": ce_data.get("totalTradedVolume", 0),
                "iv": ce_data.get("impliedVolatility", "N/A"),
                "bidPrice": ce_data.get("bidprice", "N/A"),
                "askPrice": ce_data.get("askPrice", "N/A"),
                "odinPercentage": calculate_odin_percentage(
                    ce_data.get("changeinOpenInterest", 0),
                    ce_data.get("openInterest", 0)
                )
            })

        if pe_data:
            put_data.append({
                "strikePrice": strike_price,
                "lastPrice": pe_data.get("lastPrice", "N/A"),
                "openInterest": pe_data.get("openInterest", 0),
                "changeInOI": pe_data.get("changeinOpenInterest", 0),
                "volume": pe_data.get("totalTradedVolume", 0),
                "iv": pe_data.get("impliedVolatility", "N/A"),
                "bidPrice": pe_data.get("bidprice", "N/A"),
                "askPrice": pe_data.get("askPrice", "N/A"),
                "odinPercentage": calculate_odin_percentage(
                    pe_data.get("changeinOpenInterest", 0),
                    pe_data.get("openInterest", 0)
                )
            })

    return call_data, put_data

def should_add_intraday_entry(current_minutes):
    return current_minutes % 15 == 0  # Update every 15 minutes

def parse_and_save(data, json_file, symbol):
    """Parse and save option chain data."""
    if not data or "records" not in data:
        print(f"[{pd.Timestamp.now()}] No data available for {symbol}")
        return

    underlying_value = data["records"].get("underlyingValue")
    if underlying_value is None:
        print(f"[{pd.Timestamp.now()}] Missing underlying value for {symbol}")
        return

    print(f"[{pd.Timestamp.now()}] {symbol.upper()} UNDERLYING VALUE: {underlying_value}")

    option_chain = data["records"]["data"]
    full_call_data, full_put_data = filter_call_put_data(option_chain, underlying_value, None)

    with open(json_file, 'w') as file:
        json.dump({
            "underlyingValue": underlying_value,
            "callData": full_call_data,
            "putData": full_put_data
        }, file, indent=4)

def calculate_intraday_data(call_data, put_data, current_price):
    """Calculate intraday data including VWAP and Final Signal."""
    if not call_data or not put_data:
        return {
            "Time": time.strftime("%H:%M"),
            "Call": 0,
            "Put": 0,
            "Difference": 0,
            "PCR": 0,
            "Option Signal": "Neutral",
            "VWAP": 0,
            "Price": 0,
            "VWAP Signal": "Neutral",
            "Final Signal": "Neutral"  # Add Final Signal
        }

    # Calculate Call and Put OI
    total_call_oi = sum(item['openInterest'] for item in call_data if item['openInterest'])
    total_put_oi = sum(item['openInterest'] for item in put_data if item['openInterest'])

    # Calculate PCR
    pcr = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else 0

    # Calculate Option Signal
    option_signal = "Buy" if total_call_oi > total_put_oi else "Sell"

    # Calculate VWAP
    vwap_numerator = sum(
        (item['lastPrice'] * item['openInterest']) for item in (call_data + put_data) if item['openInterest']
    )
    vwap_denominator = sum(item['openInterest'] for item in (call_data + put_data) if item['openInterest'])
    vwap = round(vwap_numerator / vwap_denominator, 2) if vwap_denominator > 0 else 0

    # Determine VWAP Signal
    vwap_signal = "Buy" if current_price > vwap else "Sell"

    # Combine Option Signal and VWAP Signal for Final Signal
    if option_signal == "Buy" and vwap_signal == "Buy":
        final_signal = "Buy"
    elif option_signal == "Sell" and vwap_signal == "Sell":
        final_signal = "Sell"
    else:
        final_signal = "Neutral"

    return {
        "Time": time.strftime("%H:%M"),
        "Call": total_call_oi,
        "Put": total_put_oi,
        "Difference": total_call_oi - total_put_oi,
        "PCR": pcr,
        "Option Signal": option_signal,
        "VWAP": vwap,
        "Price": current_price,
        "VWAP Signal": vwap_signal,
        "Final Signal": final_signal  # Include Final Signal
    }


import datetime
def periodic_fetch():
    """Fetch option chain data and update intraday data for Nifty and Bank Nifty."""
    try:
        # Fetch and update Nifty option chain data
        nifty_data = fetch_data(nifty_url, "Nifty")
        if nifty_data:
            parse_and_save(nifty_data, "nifty_option_chain.json", "Nifty")
            with open("nifty_option_chain.json", 'r') as nifty_file:
                data = json.load(nifty_file)
                call_data = data.get("callData", [])
                put_data = data.get("putData", [])
                underlying_value = data.get("underlyingValue", 0)

                # Calculate intraday data
                new_entry = calculate_intraday_data(call_data, put_data, underlying_value)

                # Get current time rounded to 15-minute interval
                now = datetime.datetime.now()
                rounded_time = (now - datetime.timedelta(minutes=now.minute % 15,
                                                         seconds=now.second,
                                                         microseconds=now.microsecond)).strftime("%H:%M")

                # Check for duplicate entry before appending
                if nifty_intraday_data and nifty_intraday_data[-1]["Time"] == rounded_time:
                    print(f"Nifty intraday data already exists for {rounded_time}. Skipping update.")
                else:
                    new_entry["Time"] = rounded_time
                    nifty_intraday_data.append(new_entry)
                    print(f"Added new Nifty intraday data for time {rounded_time}.")

        # Fetch and update Bank Nifty option chain data
        banknifty_data = fetch_data(banknifty_url, "Bank Nifty")
        if banknifty_data:
            parse_and_save(banknifty_data, "banknifty_option_chain.json", "Bank Nifty")
            with open("banknifty_option_chain.json", 'r') as banknifty_file:
                data = json.load(banknifty_file)
                call_data = data.get("callData", [])
                put_data = data.get("putData", [])
                underlying_value = data.get("underlyingValue", 0)

                # Calculate intraday data
                new_entry = calculate_intraday_data(call_data, put_data, underlying_value)

                # Get current time rounded to 15-minute interval
                now = datetime.datetime.now()
                rounded_time = (now - datetime.timedelta(minutes=now.minute % 15,
                                                         seconds=now.second,
                                                         microseconds=now.microsecond)).strftime("%H:%M")

                # Check for duplicate entry before appending
                if banknifty_intraday_data and banknifty_intraday_data[-1]["Time"] == rounded_time:
                    print(f"Bank Nifty intraday data already exists for {rounded_time}. Skipping update.")
                else:
                    new_entry["Time"] = rounded_time
                    banknifty_intraday_data.append(new_entry)
                    print(f"Added new Bank Nifty intraday data for time {rounded_time}.")
    except Exception as e:
        print(f"Error in periodic fetch: {e}")


@app.route('/api/getSignals', methods=['GET'])
def get_signals():
    try:
        with open("nifty_option_chain.json", 'r') as nifty_file:
            nifty_data = json.load(nifty_file)
        with open("banknifty_option_chain.json", 'r') as banknifty_file:
            banknifty_data = json.load(banknifty_file)

        return jsonify({
            "Nifty": nifty_data,
            "BankNifty": banknifty_data
        })
    except FileNotFoundError as e:
        return jsonify({"error": f"File not found: {str(e)}"}), 404
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
    
@app.route('/api/getCallPutData', methods=['GET'])
def get_call_put_data():
    market = request.args.get('market', '').lower()
    if market not in ['nifty', 'banknifty']:
        return jsonify({"error": "Invalid market parameter"}), 400

    file_name = f"{market}_option_chain.json"
    try:
        with open(file_name, 'r') as file:
            data = json.load(file)
            return jsonify(data)
    except FileNotFoundError:
        return jsonify({"error": f"Error fetching data for {market}: File not found"}), 404
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500

@app.route('/api/getIntradayData', methods=['GET'])
def get_intraday_data():
    try:
        # Get the market parameter from the query
        market = request.args.get("market", "").lower()
        if market not in ["nifty", "banknifty"]:
            return jsonify({"error": "Invalid market parameter"}), 400

        # Load the corresponding option chain data file
        file_name = f"{market}_option_chain.json"
        try:
            with open(file_name, 'r') as file:
                market_data = json.load(file)
        except FileNotFoundError:
            return jsonify({"error": f"File not found: {file_name}"}), 404
        except json.JSONDecodeError:
            return jsonify({"error": f"Error decoding JSON file: {file_name}"}), 500

        call_data = market_data.get("callData", [])
        put_data = market_data.get("putData", [])
        underlying_value = market_data.get("underlyingValue", 0)

        # Calculate intraday data
        new_entry = calculate_intraday_data(call_data, put_data, underlying_value)

        # Get the current time rounded to the nearest 15-minute interval
        now = datetime.datetime.now()
        rounded_time = (now - datetime.timedelta(minutes=now.minute % 15,
                                                 seconds=now.second,
                                                 microseconds=now.microsecond)).strftime("%H:%M")
        new_entry["Time"] = rounded_time

        # Check if this is a new entry and add it to the corresponding intraday_data
        if market == "nifty":
            global nifty_intraday_data
            if nifty_intraday_data and nifty_intraday_data[-1]["Time"] == rounded_time:
                logging.info(f"Duplicate intraday data for {market} at {rounded_time}. Skipping update.")
            else:
                nifty_intraday_data.append(new_entry)
                logging.info(f"Added new intraday data for {market} at {rounded_time}.")
            return jsonify(nifty_intraday_data), 200
        elif market == "banknifty":
            global banknifty_intraday_data
            if banknifty_intraday_data and banknifty_intraday_data[-1]["Time"] == rounded_time:
                logging.info(f"Duplicate intraday data for {market} at {rounded_time}. Skipping update.")
            else:
                banknifty_intraday_data.append(new_entry)
                logging.info(f"Added new intraday data for {market} at {rounded_time}.")
            return jsonify(banknifty_intraday_data), 200
    except Exception as e:
        logging.error(f"Error fetching intraday data for {market}: {e}")
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
    

if __name__ == "__main__":
    # Schedule periodic tasks
    schedule.every(1).minutes.do(periodic_fetch)  # Fetch option chain data and update intraday data every minute

    # Run scheduled tasks in a separate thread
    def run_scheduled_tasks():
        while True:
            schedule.run_pending()
            time.sleep(1)  # Avoid busy-waiting

    threading.Thread(target=run_scheduled_tasks, daemon=True).start()

    # Run Flask app
    app.run(host="0.0.0.0", port=8899)