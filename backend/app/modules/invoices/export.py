#!/usr/bin/env python3
"""
Export MySQL invoice_data table to Excel file
Downloads the latest invoice data from MySQL database and saves it as an Excel file
"""

import pandas as pd
from sqlalchemy import text
from datetime import datetime
import os
import numpy as np
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl import load_workbook


# ---------------- DB CONFIG ----------------
db_config = {
    "host": "localhost",
    "user": "root",
    "password": "Irahirs11@ittak!",
    "database": "bsr"
}


def get_db_engine():
    """Get MySQL database engine connection"""
    from backend.app.database import get_engine

    return get_engine()


def _get_invoice_table_name():
    """Return invoice table name from env INVOICE_TABLE_NAME, default invoice_data. Safe for SQL."""
    from backend.app.config import get_settings

    return get_settings().invoice_table_name


def export_invoice_data_to_excel(output_file=None, include_all_columns=True):
    """
    Export invoice_data table from MySQL to Excel file
    
    Args:
        output_file: Optional path for output Excel file. If None, generates filename with timestamp
        include_all_columns: If True, includes all columns. If False, includes only main columns.
    
    Returns:
        str: Path to the created Excel file
    """
    table_name = _get_invoice_table_name()
    print(f"📊 Exporting MySQL {table_name} to Excel...")
    
    # Generate output filename if not provided
    if output_file is None:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"invoice_data_export_{timestamp}.xlsx"
    else:
        # Extract filename from path if provided
        filename = os.path.basename(output_file)
        # Ensure .xlsx extension
        if not filename.endswith('.xlsx'):
            filename += '.xlsx'
    
    # Always save to ./invoices folder (similar to test1.py saving to ./api)
    output_dir = "invoices"
    os.makedirs(output_dir, exist_ok=True)
    
    # Build full output path
    output_file = os.path.join(output_dir, filename)
    
    try:
        # Connect to database
        engine = get_db_engine()
        print(f"✅ Connected to MySQL database: {db_config['database']}")
        
        # Query all data from invoice table (name from INVOICE_TABLE_NAME env)
        query = f"SELECT * FROM {table_name} ORDER BY id ASC"
        print(f"📥 Fetching data from {table_name} table...")
        
        df = pd.read_sql(query, engine)
        
        if df.empty:
            print(f"⚠️  No data found in {table_name} table")
            return None
        
        print(f"✅ Fetched {len(df)} rows from database")
        
        # Remove columns that shouldn't be exported (keep them in MySQL but don't export)
        columns_to_remove = ['updated_at', 'processing_date', 's_no']
        removed_cols = []
        for col in columns_to_remove:
            if col in df.columns:
                df = df.drop(columns=[col])
                removed_cols.append(col)
        
        if removed_cols:
            print(f"   ⚠️  Removed columns from export (kept in MySQL): {', '.join(removed_cols)}")
        
        # Keep created_at as stored in MySQL (same value as frontend "created at")
        if 'created_at' in df.columns:
            df['created_at'] = pd.to_datetime(df['created_at'], errors='coerce')
            if df['created_at'].dt.tz is not None:
                df['created_at'] = df['created_at'].dt.tz_localize(None)
        
        # Replace NULL/None values with "Not Found"
        print(f"🔄 Converting NULL values to 'Not Found'...")
        # Fill NaN values first
        df = df.fillna('Not Found')
        # Also replace None, numpy.nan, and string representations
        df = df.replace([np.nan, None, 'None', 'null', 'NULL', 'nan', 'NaN'], 'Not Found')
        
        # Map MySQL column names to Excel-friendly names
        column_mapping = {
            'id': 'ID',
            'email_from': 'Email From',
            'assigned_to': 'Assigned To',
            'hotel_name': 'Hotel Name',
            'guest_name': 'Guest Name',
            'booking_code': 'Booking Code',
            'bill_number': 'Bill Number',
            'bill_date': 'Bill Date',
            'check_in_date': 'Check-in Date',
            'check_out_date': 'Check-out Date',
            'amount_paid': 'Amount Paid',
            'email_amount': 'Email Amount',
            'hbd_gst_number': 'HBD GST Number',
            'client_gst_number': 'Client GST Number',
            'validation_flag': 'Validation Flag',
            'total_cgst': 'Total CGST',
            'total_sgst': 'Total SGST',
            'room_charge': 'Room Charge',
            'total_tariff': 'Total Tariff',
            'other_charges': 'Other Charges',
            'total_igst': 'Total IGST',
            'total_taxes': 'Total Taxes',
            'discount': 'Discount',
            'advance_received': 'Advance Received',
            'pdf_filename': 'PDF Filename',
            'blob_url': 'Blob URL',
            'matched_status': 'Matched Status',
            'amount_match': 'Amount Match',
            'amount_difference': 'Amount Difference',
            'created_at': 'Created At'
        }
        
        # Rename columns
        df.rename(columns=column_mapping, inplace=True)
        
        # Select columns based on include_all_columns flag
        if not include_all_columns:
            # Main columns only
            main_columns = [
                'ID', 'Created At', 'Email From', 'Assigned To',
                'Hotel Name', 'Guest Name', 'Booking Code', 'Bill Number', 'Bill Date',
                'Check-in Date', 'Check-out Date', 'Amount Paid',
                'HBD GST Number', 'Client GST Number', 'Validation Flag',
                'Total CGST', 'Total SGST',
                'Room Charge', 'Total Tariff', 'Other Charges',
                'Total IGST', 'Total Taxes', 'Discount', 'Advance Received',
                'PDF Filename', 'Blob URL',
                'Matched Status', 'Amount Match', 'Amount Difference'
            ]
            # Only include columns that exist in dataframe
            available_columns = [col for col in main_columns if col in df.columns]
            df = df[available_columns]
        
        # Reorder columns to match Excel format
        desired_column_order = [
            'ID', 'Created At', 'Email From', 'Assigned To',
            'Hotel Name', 'Guest Name', 'Booking Code', 'Bill Number', 'Bill Date',
            'Check-in Date', 'Check-out Date', 'Amount Paid',
            'HBD GST Number', 'Client GST Number', 'Validation Flag',
            'Total CGST', 'Total SGST',
            'Room Charge', 'Total Tariff', 'Other Charges',
            'Total IGST', 'Total Taxes', 'Discount', 'Advance Received',
            'PDF Filename', 'Blob URL',
            'Matched Status', 'Amount Match', 'Amount Difference'
        ]
        
        # Add any additional columns at the end
        if include_all_columns:
            additional_cols = [col for col in df.columns if col not in desired_column_order]
            desired_column_order = desired_column_order + additional_cols
        
        # Reorder columns (only include columns that exist)
        existing_columns = [col for col in desired_column_order if col in df.columns]
        remaining_columns = [col for col in df.columns if col not in existing_columns]
        df = df[existing_columns + remaining_columns]
        
        # Save to Excel with formatting
        print(f"💾 Saving to Excel file: {output_file}")
        with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Invoice Data', index=False)
            
            # Get workbook and worksheet for formatting
            workbook = writer.book
            worksheet = writer.sheets['Invoice Data']
            
            # Set column widths
            column_widths = {
                'A': 8,   # ID
                'B': 20,  # Created At
                'C': 25,  # Email From
                'D': 15,  # Assigned To
                'E': 35,  # Hotel Name
                'F': 25,  # Guest Name
                'G': 18,  # Booking Code
                'H': 18,  # Bill Number
                'I': 15,  # Bill Date
                'J': 15,  # Check-in Date
                'K': 15,  # Check-out Date
                'L': 20,  # Amount Paid
                'M': 20,  # HBD GST Number
                'N': 20,  # Client GST Number
                'O': 15,  # Total CGST
                'P': 15,  # Total SGST
                'Q': 25,  # PDF Filename
                'R': 25   # Blob URL
            }
            
            for col_letter, width in column_widths.items():
                if col_letter in worksheet.column_dimensions:
                    worksheet.column_dimensions[col_letter].width = width
            
            # Add professional formatting
            # Header formatting
            header_font = Font(bold=True, color="FFFFFF", size=11)
            header_fill = PatternFill(start_color="2F5597", end_color="2F5597", fill_type="solid")
            header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            
            # Apply header formatting
            for cell in worksheet[1]:
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_alignment
            
            # Add borders to all cells
            thin_border = Border(
                left=Side(style='thin'),
                right=Side(style='thin'),
                top=Side(style='thin'),
                bottom=Side(style='thin')
            )
            
            # Apply borders to all cells
            for row in worksheet.iter_rows():
                for cell in row:
                    cell.border = thin_border
                    if cell.row > 1:  # Data rows
                        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            
            # Freeze the header row
            worksheet.freeze_panes = "A2"
            
            # Auto-fit row heights
            for row in worksheet.iter_rows():
                max_height = 0
                for cell in row:
                    if cell.value:
                        lines = str(cell.value).count('\n') + 1
                        height = max(15, lines * 15)
                        max_height = max(max_height, height)
                if max_height > 0:
                    worksheet.row_dimensions[row[0].row].height = min(max_height, 60)
            
            # Format date columns
            date_columns = ['B', 'I', 'J', 'K']  # Created At, Bill Date, Check-in, Check-out
            for col in date_columns:
                if col in [cell.column_letter for cell in worksheet[1]]:
                    for row in range(2, worksheet.max_row + 1):
                        cell = worksheet[f'{col}{row}']
                        if cell.value and str(cell.value) != 'Not Found':
                            try:
                                cell_value = str(cell.value)
                                if '-' in cell_value or '/' in cell_value:
                                    cell.number_format = 'YYYY-MM-DD'
                            except:
                                pass
        
        engine.dispose()
        print(f"✅ Successfully exported {len(df)} rows to Excel file: {output_file}")
        print(f"📁 Full path: {os.path.abspath(output_file)}")
        
        return os.path.abspath(output_file)
        
    except Exception as e:
        print(f"❌ Error exporting data to Excel: {e}")
        import traceback
        traceback.print_exc()
        return None


def export_recent_invoice_data(days=30, output_file=None):
    """
    Export recent invoice data from MySQL (last N days) to Excel file
    
    Args:
        days: Number of days to look back (default: 30)
        output_file: Optional path for output Excel file
    
    Returns:
        str: Path to the created Excel file
    """
    print(f"📊 Exporting invoice data from last {days} days...")
    
    if output_file is None:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"invoice_data_recent_{days}days_{timestamp}.xlsx"
        # Always save to ./invoices folder
        output_dir = "invoices"
        os.makedirs(output_dir, exist_ok=True)
        output_file = os.path.join(output_dir, filename)
    else:
        # If output_file provided, ensure it goes to invoices folder
        filename = os.path.basename(output_file)
        if not filename.endswith('.xlsx'):
            filename += '.xlsx'
        output_dir = "invoices"
        os.makedirs(output_dir, exist_ok=True)
        output_file = os.path.join(output_dir, filename)
    
    try:
        table_name = _get_invoice_table_name()
        engine = get_db_engine()
        print(f"✅ Connected to MySQL database: {db_config['database']}")
        
        # Query recent data (ordered by ID ascending to match serial number order)
        query = f"""
        SELECT * FROM {table_name} 
        WHERE created_at >= DATE_SUB(NOW(), INTERVAL {days} DAY)
        ORDER BY id ASC
        """
        
        print(f"📥 Fetching data from last {days} days...")
        df = pd.read_sql(query, engine)
        
        if df.empty:
            print(f"⚠️  No data found in last {days} days")
            return None
        
        print(f"✅ Fetched {len(df)} rows from database")
        
        # Note: Column removal (id, processing_date, updated_at) and timezone conversion 
        # will be handled by export_invoice_data_to_excel() function
        # Use the same export function logic
        return export_invoice_data_to_excel(output_file=output_file, include_all_columns=True)
        
    except Exception as e:
        print(f"❌ Error exporting recent data: {e}")
        import traceback
        traceback.print_exc()
        return None


if __name__ == "__main__":
    """Run as standalone script"""
    import sys
    
    # Check command line arguments
    if len(sys.argv) > 1:
        if sys.argv[1] == '--recent':
            days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
            output_file = sys.argv[3] if len(sys.argv) > 3 else None
            export_recent_invoice_data(days=days, output_file=output_file)
        elif sys.argv[1] == '--help':
            print("Usage:")
            print("  python export_mysql_to_excel.py                    # Export all data")
            print("  python export_mysql_to_excel.py --recent [days]    # Export recent data (default: 30 days)")
            print("  python export_mysql_to_excel.py --recent 7 output.xlsx  # Export last 7 days to specific file")
    else:
        # Default: Export all data
        export_invoice_data_to_excel()

