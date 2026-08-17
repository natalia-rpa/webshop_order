import pandas as pd
from auth import init_connections
from config_loader import load_config

def test_spreadsheet_read():
    print("Loading config and connecting to Google Sheets...")
    config = load_config()
    
    # Connect to Google Sheets
    sheets_client, main_sheet = init_connections(config)
    print(f"Connected successfully to sheet: {main_sheet.title}\n")
    
    values = main_sheet.get_all_values()
    if not values:
        print("The sheet is completely empty!")
        return
        
    headers = values[0]
    print("--- 1. RAW HEADERS FROM SHEET ---")
    # CHANGED: Print headers exactly as they come from Google Sheets with quotes to expose hidden spaces
    print([f"'{h}'" for h in headers])
    
    # Strip whitespaces as we do in spreadsheet_processing.py
    stripped_headers = [str(h).strip() for h in headers]
    
    # Load into Pandas DataFrame
    df = pd.DataFrame(values[1:], columns=stripped_headers)
    df["row_number"] = df.index + 2
    
    # Attempt to rename columns exactly as in our business logic
    df = df.rename(columns={
        "CLIENT_NUMBER": "client_number",
        "CLIENT_NAME": "client_name",
        "EMAIL_ID": "email_id",
        "MAIL": "client_mail",
        "ATTACHMENTS_PATH": "attachments_path",
        "MANUAL_PHASE": "manual_phase",
        "ROBOT_PHASE": "robot_phase",
        "EMAIL_TITLE": "email_title",
        "ACTIVE_PHASE": "active_phase"
    })
    
    print("\n--- 2. PANDAS DATAFRAME COLUMNS AFTER RENAMING ---")
    print(df.columns.tolist())
    
    print("\n--- 3. FIRST 10 ROWS OF DATA (Selected Columns) ---")
    # Display only the most relevant columns for debugging
    cols_to_show = ["row_number"]
    for col in ["email_id", "client_number", "client_name", "robot_phase"]:
        if col in df.columns:
            cols_to_show.append(col)
        elif col.upper() in df.columns:
            # Fallback if renaming failed
            cols_to_show.append(col.upper())
            
    print(df[cols_to_show].head(10).to_string(index=False))
    
    print("\n--- 4. DIAGNOSIS ---")
    if "client_number" not in df.columns:
        print("ERROR: 'client_number' is missing from Pandas columns!")
        print("This means the exact string 'CLIENT_NUMBER' was not found in your stripped headers.")
    else:
        empty_count = len(df[df["client_number"].str.strip() == ""])
        print(f"Column 'client_number' exists. Found {empty_count} rows where it is completely empty.")

if __name__ == "__main__":
    test_spreadsheet_read()