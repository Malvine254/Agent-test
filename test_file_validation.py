"""
Test file upload validation logic
"""
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

from config import Config

def test_file_validation():
    """Test file type and size validation"""
    
    # Mock attachment class for testing
    class MockAttachment:
        def __init__(self, name, size_bytes=None):
            self.name = name
            self.content = {'fileSize': size_bytes} if size_bytes else {}
    
    # Import validation function from app
    try:
        from app import validate_file_attachment
    except ImportError:
        print("Could not import validate_file_attachment from app.py")
        return
    
    print("Testing file upload validation...")
    print(f"Max size: {Config.MAX_FILE_SIZE_MB}MB")
    print(f"Allowed types: {len(Config.ALLOWED_FILE_TYPES)} types")
    print(f"Blocked types: {len(Config.BLOCKED_FILE_TYPES)} types")
    print()
    
    # Test cases
    test_cases = [
        # Valid files
        ("document.pdf", 1024*1024, True, "Valid PDF"),
        ("spreadsheet.xlsx", 5*1024*1024, True, "Valid Excel"),
        ("image.jpg", 2*1024*1024, True, "Valid image"),
        ("data.csv", 1024, True, "Valid CSV"),
        
        # Invalid file types
        ("virus.exe", 1024, False, "Blocked executable"),
        ("script.ps1", 1024, False, "Blocked script"),
        ("program.bat", 1024, False, "Blocked batch file"),
        ("unknown.xyz", 1024, False, "Unsupported type"),
        
        # Size violations (assuming 20MB limit)
        ("large.pdf", 25*1024*1024, False, "File too large"),
        ("huge.docx", 50*1024*1024, False, "Way too large"),
    ]
    
    for filename, size_bytes, should_pass, description in test_cases:
        attachment = MockAttachment(filename, size_bytes)
        is_valid, error_msg = validate_file_attachment(attachment)
        
        status = "✅ PASS" if is_valid == should_pass else "❌ FAIL"
        result = "VALID" if is_valid else f"INVALID: {error_msg}"
        
        print(f"{status} {filename:15} ({description:20}) -> {result}")
    
    print("\nValidation test completed.")

if __name__ == "__main__":
    test_file_validation()