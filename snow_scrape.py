import json
import csv
import time
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# File to store our login session
STATE_FILE = "state.json"

def capture_opensnow_stats():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # Check if we have a saved session
        if os.path.exists(STATE_FILE):
            print("Loading existing session...")
            context = browser.new_context(
                storage_state=STATE_FILE,
                viewport={'width': 1280, 'height': 1200}
            )
        else:
            print("No session found. Need to login...")
            context = browser.new_context(viewport={'width': 1280, 'height': 1200})

        page = context.new_page()

    	# --- 1. CONDITIONAL LOGIN FLOW ---
        page.goto("https://opensnow.com/user/login")
        time.sleep(2)

        # If we are STILL on the login page, we need to log in
        if "user/login" in page.url:
            print("Performing login...")
            email_field = page.locator("input.tw-inline-block")
            email_field.wait_for(state="visible")

            email_field.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            email_field.press_sequentially("NotReallyMyEmail@gmail.com", delay=150)
            page.keyboard.press("Enter")

            time.sleep(3)
            page.wait_for_selector("input.tw-inline-block")

            pw_field = page.locator("input.tw-inline-block")
            pw_field.press_sequentially("NotReallyMyPassword", delay=100)
            page.keyboard.press("Enter")

            # Wait for the URL to change away from 'login'
            try:
                page.wait_for_function("() => !window.location.href.includes('user/login')", timeout=15000)
                context.storage_state(path=STATE_FILE)
                print(f"--- Session saved. Logged in at: {page.url} ---")
            except:
                print("Redirect check failed. Check error_state.png")
                page.screenshot(path="error_state.png")
                return
        else:
            print(f"--- Reused session. Already at: {page.url} ---")


        # --- 2. NAVIGATE TO SNOW SUMMARY ---
        page.goto("https://opensnow.com/location/winterpark/snow-summary")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(5)

        # --- 3. DATA EXTRACTION WITH DATE CONVERSION ---
        try:
            # Extract the raw payload string from the page context
            raw_payload_str = page.evaluate("""
                async () => {
                    const getStore = () => useNuxtApp().payload.data["locationStore-fetchForecastSnowDetail-winterpark-imperial"];
                    for (let i = 0; i < 20; i++) {
                        const s = getStore();
                        if (s && s.forecast_snow_daily && s.forecast_snow_daily[0].precip_snow !== null) break;
                        await new Promise(r => setTimeout(r, 500));
                    }
                    return JSON.stringify(useNuxtApp().payload.data);
                }
            """)

            payload = json.loads(raw_payload_str)
            wp_data = payload.get("locationStore-fetchForecastSnowDetail-winterpark-imperial", {})
            forecast = wp_data.get('forecast_snow_daily', [])
            history = wp_data.get('history_snow_daily', [])

            def format_to_mt_date(utc_string):
                if not utc_string: return "N/A"
                utc_dt = datetime.fromisoformat(utc_string.replace('Z', '+00:00'))
                mt_dt = utc_dt.astimezone(ZoneInfo("America/Denver"))
                return mt_dt.strftime('%Y-%m-%d')

            # 1. Determine Today and Yesterday in Mountain Time
            now_mt = datetime.now(ZoneInfo("America/Denver"))
            today_str = now_mt.strftime('%Y-%m-%d')
            yesterday_str = (now_mt - timedelta(days=1)).strftime('%Y-%m-%d')

            # 2. Extract today's history value to merge it backwards
            today_history_amt = 0.0
            filtered_history = []

            for d in history:
                date_str = format_to_mt_date(d.get('display_at'))
                if date_str == today_str:
                    # Capture the 2" reported "today" in the history bucket
                    today_history_amt = float(d.get('precip_snow') or 0)
                else:
                    filtered_history.append(d)

            # 3. Process History and Merge today's recorded snow into yesterday's bar
            data_rows = []
            for d in filtered_history:
                date_str = format_to_mt_date(d.get('display_at'))
                amt = float(d.get('precip_snow') or 0)

                # Merge logic: Add today's historical amount to yesterday's (2/3) slot
                if date_str == yesterday_str:
                    amt += today_history_amt

                data_rows.append(["last", date_str, d.get('display_at_local_label'), amt])

            # 4. Add Forecast (keeping today's forecast entry in the 'next' bucket)
            for d in forecast:
                data_rows.append(["next", format_to_mt_date(d.get('display_at')), d.get('display_at_local_label'), d.get('precip_snow') or 0])

            # 5. Write to CSV
            with open('snow_report.csv', 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['type', 'date', 'day_label', 'amount'])
                writer.writerows(data_rows)

            # 6. Build HA JSON with pre-calculated totals
            history_final = [{"x": row[1], "y": float(row[3])} for row in data_rows if row[0] == "last"]
            forecast_final = [{"x": row[1], "y": float(row[3])} for row in data_rows if row[0] == "next"]

            # Calculate specific totals for the HA headers
            # History: Sum the last 3 days available in history (e.g., Feb 1, 2, 3)
            h_sum_3d = sum(item['y'] for item in history_final[-3:]) if len(history_final) >= 3 else sum(item['y'] for item in history_final)

            # Forecast: Sum the first 3 days of the forecast list (e.g., Feb 4, 5, 6)
            f_sum_3d = sum(item['y'] for item in forecast_final[:3]) if len(forecast_final) >= 3 else sum(item['y'] for item in forecast_final)

            ha_data = {
                "history": history_final,
                "forecast": forecast_final,
		        "last_24h": history_final[-1]['y'] if history_final else 0,
                "f_today": forecast_final[0]['y'] if len(forecast_final) > 0 else 0,
                "f_tomorrow": forecast_final[1]['y'] if len(forecast_final) > 1 else 0,
		        "f_total_2d": sum(item['y'] for item in forecast_final[:2]),
                "h_total_3d": h_sum_3d,
                "f_total_3d": f_sum_3d,
                "f_total_5d": sum(item['y'] for item in forecast_final[:5]),
                "f_total_7d": sum(item['y'] for item in forecast_final[:7])
            }

            with open('snow_ha.json', 'w') as f:
                json.dump(ha_data, f)

            print(f"Success! Today's history ({today_history_amt} in) merged into {yesterday_str}.")

        except Exception as e:
            print(f"Extraction failed: {e}")

if __name__ == "__main__":
    capture_opensnow_stats()
