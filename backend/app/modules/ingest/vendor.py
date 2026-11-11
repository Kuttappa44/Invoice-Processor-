"""Assign vendor folders and upload PDFs to Blob."""

from __future__ import annotations

import re

from backend.app.integrations.azure_blob import upload_pdf_bytes_to_blob_storage


def find_vendor_column(vendor_name, vendor_reference):
    """Find the appropriate assignee for a vendor using exact matching only."""
    if not vendor_name or vendor_name == 'Unknown Vendor':
        return 'UNASSIGNED'
    
    print(f"   🔍 Looking for vendor: '{vendor_name}'")
    
    # Direct match only (exact match)
    if vendor_name.lower() in vendor_reference:
        result = vendor_reference[vendor_name.lower()]
        print(f"   ✅ Direct match found: {result}")
        return result

    normalized_key = re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', vendor_name.lower())).strip()
    if normalized_key and normalized_key in vendor_reference:
        result = vendor_reference[normalized_key]
        print(f"   ✅ Normalized match found: {result}")
        return result
    
    print(f"   ❌ No exact match found for: '{vendor_name}'")
    return 'UNASSIGNED'


def get_drive_folder_name(excel_column):
    """Convert Excel column name to Google Drive folder name"""
    # Now MOKSHITHA maps directly to MOKSHITHA folder
    return excel_column


def upload_pdf_to_blob_storage(pdf_data, assigned_column):
    """Upload PDF file to Azure Blob Storage with vendor folder prefix"""
    if not pdf_data.get('payload'):
        return None
    
    try:
        # Get PDF bytes
        pdf_bytes = pdf_data['payload']
        original_filename = pdf_data.get('filename', 'unknown.pdf')
        
        # Determine folder prefix (vendor name) - convert Excel column to folder prefix
        folder_prefix = get_drive_folder_name(assigned_column) if assigned_column != 'Not Found' else 'UNASSIGNED'
        print(f"   🔧 DEBUG: assigned_column='{assigned_column}', folder_prefix='{folder_prefix}'")
        print(f"   📄 Original filename: {original_filename}")
        
        # Upload to Azure Blob Storage with folder prefix
        blob_url = upload_pdf_bytes_to_blob_storage(
            pdf_bytes=pdf_bytes,
            filename=original_filename,
            folder_prefix=folder_prefix
        )
        
        if blob_url:
            print(f"   ☁️  Uploaded PDF to Blob Storage: {folder_prefix}/{original_filename}")
            return blob_url
        else:
            print(f"   ⚠️  Failed to upload PDF to Blob Storage")
            return None
            
    except Exception as e:
        print(f"   ❌ Error uploading PDF to Blob Storage: {e}")
        import traceback
        traceback.print_exc()
        return None
