"""Booking-list API client (Warsoft unmapped invoices) and Excel export."""

import os
import requests
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime

from backend.app.shared.run_excels import run_excel_dir

# Load environment variables from .env file
load_dotenv()

def fetch_single_page(api_endpoint, access_token, page_no, headers):
    """
    Fetch a single page of data from the API.
    Returns the JSON response data.
    """
    # Request body with page number (API expects pageNo as string)
    json_data = {"pageNo": str(page_no)}
    
    try:
        response = requests.post(api_endpoint, headers=headers, json=json_data)
        response.raise_for_status()  # Raise an error for bad status codes
        
        data = response.json()
        return data
        
    except requests.exceptions.RequestException as e:
        print(f"Error fetching page {page_no} from API: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response status code: {e.response.status_code}")
            print(f"Response body: {e.response.text[:500]}")  # First 500 chars
        raise

def fetch_api_data():
    """
    Fetch all pages of data from the API endpoint using the access token.
    Automatically loops through all pages until no more data is available.
    Returns the combined JSON response data from all pages.
    """
    api_endpoint = os.getenv('API_ENDPOINT')
    access_token = os.getenv('ACCESS_TOKEN')
    
    if not api_endpoint:
        raise ValueError("API_ENDPOINT not found in .env file")
    if not access_token:
        raise ValueError("ACCESS_TOKEN not found in .env file")
    
    # Set up headers with authorization
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    
    print(f"Fetching data from: {api_endpoint}")
    print("Fetching all pages...")
    
    all_data = []
    page_no = 1
    has_more_data = True
    
    while has_more_data:
        print(f"Fetching page {page_no}...", end=" ")
        
        try:
            page_data = fetch_single_page(api_endpoint, access_token, page_no, headers)
            
            # Extract the data array from the response
            # Based on the error message, the response has "unmappedInvoices" key
            page_items = []
            
            if isinstance(page_data, dict):
                # Try to find the data array in common keys
                for key in ['unmappedInvoices', 'data', 'bookings', 'results', 'items', 'records']:
                    if key in page_data and isinstance(page_data[key], list):
                        page_items = page_data[key]
                        break
            elif isinstance(page_data, list):
                page_items = page_data
            
            # Check if we got any data
            if page_items and len(page_items) > 0:
                all_data.extend(page_items)
                print(f"✓ Found {len(page_items)} records (Total: {len(all_data)})")
                page_no += 1
            else:
                print(f"✓ No more data")
                has_more_data = False
                
        except requests.exceptions.RequestException as e:
            # If we get an error, stop fetching
            print(f"✗ Error occurred")
            raise
    
    print(f"\nSuccessfully fetched {len(all_data)} total records from {page_no - 1} page(s)")
    
    # Return the combined data in a structure similar to the original response
    # This helps maintain compatibility with the extract_booking_data function
    return {
        'unmappedInvoices': all_data,
        'totalRecords': len(all_data),
        'totalPages': page_no - 1
    }

def extract_booking_data(api_response):
    """
    Extract booking data from the API response.
    Handles different possible response structures.
    """
    # If the response is a list directly
    if isinstance(api_response, list):
        return api_response
    
    # If the response is a dict, try common keys
    if isinstance(api_response, dict):
        # Try to find the data array in common keys (including unmappedInvoices)
        for key in ['unmappedInvoices', 'data', 'bookings', 'results', 'items', 'records']:
            if key in api_response and isinstance(api_response[key], list):
                return api_response[key]
        
        # If no common key found, return the whole dict (might be a single object)
        # Or check if it's already the structure we need
        if 'bookingCode' in api_response:
            return [api_response]
    
    # If we can't find the data, return empty list
    print("Warning: Could not find booking data array in response")
    return []

def export_to_excel(data, filename=None):
    """
    Export the booking data to an Excel file.
    """
    if not data:
        print("No data to export to Excel")
        return
    
    # Create DataFrame from the data
    df = pd.DataFrame(data)
    
    # One folder per mailbox instance (api_a, api_b, api_c) so parallel runs do not share files.
    output_dir = str(run_excel_dir("api"))
    os.makedirs(output_dir, exist_ok=True)

    # Generate filename with timestamp if not provided
    if filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"booking_data_{timestamp}.xlsx"
    
    # Ensure .xlsx extension
    if not filename.endswith('.xlsx'):
        filename += '.xlsx'
    
    # Get the full absolute path
    full_path = os.path.abspath(filename)
    
    # Export to Excel (inside ./api)
    output_path = os.path.join(output_dir, filename)
    df.to_excel(output_path, index=False, engine='openpyxl')
    print(f"Data successfully exported to {output_path}")
    print(f"Total records exported: {len(df)}")
    print(f"Full file path: {os.path.abspath(output_path)}")
    
    return os.path.abspath(output_path)

def main():
    """
    Main function to orchestrate the data fetching and Excel export.
    Returns the path to the generated Excel file, or None if failed.
    """
    try:
        # Fetch data from API
        api_response = fetch_api_data()
        
        # Extract booking data
        booking_data = extract_booking_data(api_response)
        
        if not booking_data:
            print("No booking data found in API response")
            return None
        
        print(f"Found {len(booking_data)} booking records")
        
        # Export to Excel
        excel_file = export_to_excel(booking_data)
        
        print(f"\n{'='*60}")
        print(f"Process completed successfully!")
        print(f"Excel file created: {os.path.basename(excel_file)}")
        print(f"File location: {excel_file}")
        print(f"{'='*60}")
        
        return excel_file
        
    except Exception as e:
        print(f"An error occurred: {e}")
        import traceback
        traceback.print_exc()
        return None

if __name__ == "__main__":
    main()