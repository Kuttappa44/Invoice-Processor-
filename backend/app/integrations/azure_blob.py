"""Azure Blob Storage: SAS upload and URL sanitization. No invoice field mapping."""

import os
from azure.storage.blob import BlobServiceClient
from urllib.parse import urlparse
from datetime import datetime


def sanitize_blob_url(url: str) -> str:
    """
    Remove SAS tokens / query params and fragments only.
    Keeps the full blob path (including multiple ".pdf" substrings in the name).
    This prevents leaking sensitive SAS signatures in persisted data or API payloads.
    """
    if not url:
        return url
    u = str(url).strip()
    if not u:
        return u
    for sep in ("?", "#"):
        if sep in u:
            u = u.split(sep, 1)[0]
    return u

def format_blob_name(filename):
    """
    Format the blob name as datetime_filename with spaces replaced by hyphens.
    
    Args:
        filename (str): Original filename
    
    Returns:
        str: Formatted filename in "YYYY-MM-DD_HH:MM_filename" format with spaces replaced by hyphens
    """
    # Get current datetime in format: YYYY-MM-DD_HH:MM (without seconds)
    now = datetime.now()
    datetime_prefix = now.strftime("%Y-%m-%d_%H:%M")
    
    # Get the base filename (without path)
    base_name = os.path.basename(filename)
    
    # Replace spaces with hyphens
    formatted_name = base_name.replace(" ", "-")
    
    # Combine: datetime_filename
    blob_name = f"{datetime_prefix}_{formatted_name}"
    
    return blob_name


def upload_pdf_to_blob_storage(pdf_file_path, blob_name=None, sas_url=None):
    """
    Upload a PDF file to Azure Blob Storage using a SAS URL.
    The blob name will be automatically formatted as "datetime_filename" with spaces replaced by hyphens.
    
    Args:
        pdf_file_path (str): Path to the PDF file to upload
        blob_name (str, optional): Custom name for the blob in storage. If not provided, uses datetime_filename format
        sas_url (str, optional): SAS URL for the blob container. If not provided, uses the default one
    
    Returns:
        str: URL of the uploaded blob
    """
    # Default SAS URL if not provided
    if sas_url is None:
        sas_url = os.getenv('BLOB_STORAGE_SAS_URL')
    
    # Validate file exists
    if not os.path.exists(pdf_file_path):
        raise FileNotFoundError(f"PDF file not found: {pdf_file_path}")
    
    # Validate it's a PDF file
    if not pdf_file_path.lower().endswith('.pdf'):
        raise ValueError("File must be a PDF file (.pdf extension)")
    
    # Format blob name: datetime_filename with spaces replaced by hyphens
    if blob_name is None:
        blob_name = format_blob_name(pdf_file_path)
    else:
        # If custom name provided, still replace spaces with hyphens
        blob_name = blob_name.replace(" ", "-")
    
    # Parse the SAS URL to extract container URL and SAS token
    parsed_url = urlparse(sas_url)
    container_name = parsed_url.path.strip('/').split('/')[-1] if parsed_url.path else 'receipts'
    account_url = f"https://{parsed_url.netloc}"
    sas_token = parsed_url.query
    
    # Create container client with SAS token
    from azure.storage.blob import ContainerClient
    container_client = ContainerClient(account_url, container_name, credential=sas_token)
    
    # Get blob client from container client
    blob_client = container_client.get_blob_client(blob_name)
    
    # Upload the PDF file
    print(f"Uploading {pdf_file_path} to blob storage as {blob_name}...")
    

    
    with open(pdf_file_path, "rb") as data:
        blob_client.upload_blob(data, overwrite=True)
    
    # Construct the full URL of the uploaded blob
    container_url = f"{account_url}/{container_name}"
    uploaded_blob_url = f"{container_url}/{blob_name}?{sas_token}"
    safe_url = sanitize_blob_url(uploaded_blob_url)
    
    print(f"Successfully uploaded PDF to: {safe_url}")
    return safe_url


def upload_pdf_simple(pdf_file_path, blob_name=None):
    """
    Simplified function to upload PDF using the default SAS URL.
    The blob name will be automatically formatted as "datetime_filename" with spaces replaced by hyphens.
    
    Args:
        pdf_file_path (str): Path to the PDF file to upload
        blob_name (str, optional): Custom name for the blob in storage. If not provided, uses datetime_filename format
    
    Returns:
        str: URL of the uploaded blob
    """
    return upload_pdf_to_blob_storage(pdf_file_path, blob_name)


def upload_pdf_bytes_to_blob_storage(pdf_bytes, filename, folder_prefix=None, blob_name=None, sas_url=None):
    """
    Upload PDF bytes directly to Azure Blob Storage using a SAS URL.
    The blob name will be automatically formatted with folder prefix and date.
    
    Args:
        pdf_bytes (bytes): PDF file content as bytes
        filename (str): Original filename (used for naming)
        folder_prefix (str, optional): Folder prefix to organize blobs (e.g., "KUMAR", "MOKSHITHA")
        blob_name (str, optional): Custom name for the blob in storage. If not provided, uses datetime_filename format
        sas_url (str, optional): SAS URL for the blob container. If not provided, uses the default one
    
    Returns:
        str: URL of the uploaded blob, or None if upload fails
    """
    import io
    
    # Default SAS URL if not provided
    if sas_url is None:
        sas_url = os.getenv('BLOB_STORAGE_SAS_URL')
    
    if not sas_url:
        raise ValueError("BLOB_STORAGE_SAS_URL environment variable not set")
    
    # Validate it's PDF bytes (basic check)
    if not pdf_bytes or len(pdf_bytes) < 4:
        raise ValueError("Invalid PDF bytes")
    
    # Create safe filename
    base_name = os.path.basename(filename)
    safe_filename = base_name.replace(" ", "-").replace("'", "").replace('"', '')
    safe_filename = "".join(c for c in safe_filename if c.isalnum() or c in ".-_")
    
    # Format blob name with folder prefix and date
    if blob_name is None:
        # Get current datetime in format: YYYY-MM-DD_HH:MM
        now = datetime.now()
        datetime_prefix = now.strftime("%Y-%m-%d_%H:%M")
        formatted_name = safe_filename
        blob_name = f"{datetime_prefix}_{formatted_name}"
    else:
        # If custom name provided, still replace spaces with hyphens
        blob_name = blob_name.replace(" ", "-")
    
    # Add folder prefix if provided (simulates folder structure using blob name prefix)
    if folder_prefix:
        folder_prefix = folder_prefix.replace(" ", "-").replace("/", "-")
        blob_name = f"{folder_prefix}/{blob_name}"
    
    # Parse the SAS URL to extract container URL and SAS token
    parsed_url = urlparse(sas_url)
    container_name = parsed_url.path.strip('/').split('/')[-1] if parsed_url.path else 'receipts'
    account_url = f"https://{parsed_url.netloc}"
    sas_token = parsed_url.query
    
    # Create container client with SAS token
    from azure.storage.blob import ContainerClient
    container_client = ContainerClient(account_url, container_name, credential=sas_token)
    
    # Check for existing blob and rename if needed to prevent collisions
    final_blob_name = blob_name
    counter = 0
    max_attempts = 100  # Prevent infinite loop
    
    while counter < max_attempts:
        blob_client = container_client.get_blob_client(final_blob_name)
        
        # Check if blob already exists
        if blob_client.exists():
            # Blob exists, generate new name with counter
            counter += 1
            
            # Extract the filename part from final_blob_name (after last /)
            if '/' in final_blob_name:
                folder_part = final_blob_name.rsplit('/', 1)[0]
                filename_part = final_blob_name.rsplit('/', 1)[1]
            else:
                folder_part = None
                filename_part = final_blob_name
            
            # Extract file extension and base name from filename_part
            if '.' in filename_part:
                name_part, ext_part = filename_part.rsplit('.', 1)
                new_filename_part = f"{name_part}-{counter}.{ext_part}"
            else:
                new_filename_part = f"{filename_part}-{counter}"
            
            # Reconstruct blob name with new filename
            if folder_part:
                final_blob_name = f"{folder_part}/{new_filename_part}"
            else:
                final_blob_name = new_filename_part
            
            print(f"   ⚠️  Blob already exists, trying '{final_blob_name}' (attempt {counter})...")
        else:
            # Blob doesn't exist, use this name
            break
    
    if counter >= max_attempts:
        raise ValueError(f"Could not find unique blob name after {max_attempts} attempts. Last tried: {final_blob_name}")
    
    # Get blob client for final name
    blob_client = container_client.get_blob_client(final_blob_name)
    
    # Upload the PDF bytes
    if counter > 0:
        print(f"   ✅ Using unique name: {final_blob_name}")
    print(f"Uploading PDF bytes ({len(pdf_bytes)} bytes) to blob storage as {final_blob_name}...")
    
    blob_client.upload_blob(io.BytesIO(pdf_bytes), overwrite=True)
    
    # Construct the full URL of the uploaded blob (use final_blob_name, not original blob_name)
    container_url = f"{account_url}/{container_name}"
    uploaded_blob_url = f"{container_url}/{final_blob_name}?{sas_token}"
    safe_url = sanitize_blob_url(uploaded_blob_url)
    
    print(f"Successfully uploaded PDF to: {safe_url}")
    return safe_url


def upload_image_to_blob_storage(image_path, blob_name=None):
    """
    Upload an image file to Azure Blob Storage and return its public URL.

    Args:
        image_path (str): Path to the image file
        blob_name (str, optional): Custom name for the blob. If not provided, uses datetime_filename format

    Returns:
        str: Public URL of the uploaded image
    """
    import uuid

    sas_url = os.getenv("BLOB_STORAGE_SAS_URL")
    if not sas_url:
        raise ValueError("BLOB_STORAGE_SAS_URL environment variable not set")

    if blob_name is None:
        now = datetime.now()
        datetime_prefix = now.strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        base_name = os.path.basename(image_path)
        safe_name = base_name.replace(" ", "-").replace("'", "").replace('"', "")
        safe_name = "".join(c for c in safe_name if c.isalnum() or c in ".-_")
        blob_name = f"temp_images/{datetime_prefix}_{unique_id}_{safe_name}"
    else:
        blob_name = blob_name.replace(" ", "-")

    parsed_url = urlparse(sas_url)
    container_name = parsed_url.path.strip("/").split("/")[-1] if parsed_url.path else "receipts"
    account_url = f"https://{parsed_url.netloc}"
    sas_token = parsed_url.query

    from azure.storage.blob import ContainerClient

    container_client = ContainerClient(account_url, container_name, credential=sas_token)
    blob_client = container_client.get_blob_client(blob_name)

    with open(image_path, "rb") as data:
        blob_client.upload_blob(data, overwrite=True)

    container_url = f"{account_url}/{container_name}"
    image_url = f"{container_url}/{blob_name}?{sas_token}"
    return image_url


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python upload_pdf.py <path_to_pdf_file> [blob_name]")
        print("\nNote: If blob_name is not provided, it will be formatted as 'YYYY-MM-DD_HH:MM_filename'")
        print("      with spaces replaced by hyphens.")
        print("\nExample:")
        print("  python upload_pdf.py receipt.pdf")
        print("  # Will upload as: 2025-12-09_12:05_receipt.pdf")
        print("\n  python upload_pdf.py 'My Receipt.pdf' custom_name.pdf")
        print("  # Will upload as: custom-name.pdf (spaces replaced with hyphens)")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    blob_name = sys.argv[2] if len(sys.argv) > 2 else None
    
    try:
        url = upload_pdf_simple(pdf_path, blob_name)
        print(f"\nUpload successful!")
        print(f"Blob URL: {url}")
    except Exception as e:
        print(f"Error uploading PDF: {e}")
        sys.exit(1)

