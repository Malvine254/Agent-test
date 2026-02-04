# File Upload Configuration

## Overview
Your Teams bot now includes comprehensive file upload restrictions for security and performance.

## Current Limits

### File Size
- **Maximum:** 20MB per file
- **Environment Variable:** `MAX_FILE_SIZE_MB=20`

### Allowed File Types

#### Documents
- `.pdf` - PDF documents
- `.docx`, `.doc` - Microsoft Word
- `.pptx`, `.ppt` - Microsoft PowerPoint  
- `.xlsx`, `.xls` - Microsoft Excel

#### Text Files
- `.txt` - Plain text
- `.csv` - Comma-separated values
- `.json` - JSON data
- `.xml` - XML documents
- `.md` - Markdown

#### Images
- `.jpg`, `.jpeg` - JPEG images
- `.png` - PNG images
- `.gif` - GIF images
- `.bmp` - Bitmap images
- `.tiff` - TIFF images

#### Archives (Optional)
- `.zip` - ZIP archives
- `.rar` - RAR archives

### Blocked File Types (Security)

#### Executable Files
- `.exe`, `.bat`, `.cmd`, `.com`, `.scr`, `.msi`

#### Script Files
- `.ps1`, `.vbs`, `.js`, `.jar`

#### System Files
- `.dll`, `.sys`, `.ini`

## Configuration

### Environment Variables
Add to your `.env` file:

```env
# File upload limits
MAX_FILE_SIZE_MB=20

# To modify allowed types, edit Config.ALLOWED_FILE_TYPES in config.py
# To add blocked types, edit Config.BLOCKED_FILE_TYPES in config.py
```

### Customizing File Types
Edit `src/config.py` to modify the allowed/blocked file type sets:

```python
Config.ALLOWED_FILE_TYPES = {
    # Add or remove extensions as needed
    '.pdf', '.docx', '.xlsx', '.txt'
}

Config.BLOCKED_FILE_TYPES = {
    # Add dangerous file types here
    '.exe', '.bat', '.ps1'
}
```

## Error Messages
When users upload invalid files, they'll see helpful error messages:

- **Wrong file type:** "❌ File type '.xyz' is not supported. Allowed: .pdf, .docx, .xlsx..."
- **File too large:** "❌ File size (25.3MB) exceeds limit of 20MB."
- **Security blocked:** "❌ File type '.exe' is not allowed for security reasons."

## Implementation Details

### Validation Layers
1. **Bot Framework Level** - `supportsFiles: true` in manifest
2. **App Level** - File type and size validation in `app.py`
3. **Handler Level** - Content extraction in `simple_file_handler.py`

### Security Features
- Blocked executable and script files
- File size limits prevent resource exhaustion
- Content-type validation
- Safe file processing with error handling

## Testing
To test file restrictions:
1. Upload a supported file (should work)
2. Upload a blocked file type (should show error)
3. Upload a large file >20MB (should show size error)

The bot will show validation errors immediately before processing begins.