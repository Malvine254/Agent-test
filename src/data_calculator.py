"""
Data Calculator Module
Performs accurate calculations on CSV/Excel data using actual Python code.
This overcomes LLM arithmetic limitations by executing real calculations.
"""

import csv
import io
import re
import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def detect_calculation_intent(user_text: str) -> tuple[bool, str]:
    """
    Detect if user is asking for a calculation on data.
    Returns (is_calculation, calculation_type)
    """
    text = user_text.lower().strip()
    
    # Sum/Total patterns
    sum_patterns = [
        r'\b(sum|total|add up|calculate|grand total|combined|altogether)\b',
        r'\b(how much|how many)\b.*\b(total|hours|amount)\b',
        r'\btotal\s+(hours|amount|cost|value|count)\b',
        r'\bsum\s+(of|all|the)\b',
    ]
    
    # Average patterns
    avg_patterns = [
        r'\b(average|avg|mean)\b',
    ]
    
    # Count patterns
    count_patterns = [
        r'\b(count|how many|number of)\b',
    ]
    
    # Group by patterns
    group_patterns = [
        r'\b(by|per|each|breakdown|group)\b.*\b(person|user|name|contributor|team member)\b',
        r'\b(person|user|name|contributor|team member)\b.*\b(total|sum|hours)\b',
    ]
    
    # Max/Min patterns
    minmax_patterns = [
        r'\b(highest|lowest|most|least|max|min|maximum|minimum|top|bottom)\b',
    ]
    
    for pattern in sum_patterns:
        if re.search(pattern, text):
            return True, "sum"
    
    for pattern in avg_patterns:
        if re.search(pattern, text):
            return True, "average"
    
    for pattern in count_patterns:
        if re.search(pattern, text):
            return True, "count"
    
    for pattern in group_patterns:
        if re.search(pattern, text):
            return True, "group_sum"
    
    for pattern in minmax_patterns:
        if re.search(pattern, text):
            return True, "minmax"
    
    return False, ""


def parse_csv_content(content: str) -> tuple[list[dict], list[str]]:
    """Parse CSV content string into list of row dicts and column names."""
    try:
        # Clean content - remove any leading/trailing whitespace
        content = content.strip()
        
        # Try to find the CSV data (might have prefix text)
        lines = content.split('\n')
        
        # Find header line (look for comma-separated values)
        header_idx = 0
        for i, line in enumerate(lines):
            if ',' in line and not line.startswith('[') and not line.startswith('#'):
                header_idx = i
                break
        
        csv_content = '\n'.join(lines[header_idx:])
        
        reader = csv.DictReader(io.StringIO(csv_content))
        rows = list(reader)
        columns = reader.fieldnames or []
        
        return rows, columns
    except Exception as e:
        logger.error(f"Failed to parse CSV: {e}")
        return [], []


def find_numeric_column(columns: list[str], hint: str = "") -> Optional[str]:
    """Find the most likely numeric column for calculations."""
    hint_lower = hint.lower()
    
    # Priority columns based on common naming
    priority_names = ['hours', 'amount', 'total', 'value', 'cost', 'quantity', 'count', 'price', 'salary', 'wage']
    
    # First, check if hint matches a column
    for col in columns:
        if hint_lower and hint_lower in col.lower():
            return col
    
    # Then check priority names
    for name in priority_names:
        for col in columns:
            if name in col.lower():
                return col
    
    # Return first numeric-looking column
    return None


def find_groupby_column(columns: list[str]) -> Optional[str]:
    """Find the most likely column to group by (usually name/person)."""
    priority_names = ['fullname', 'name', 'person', 'contributor', 'user', 'employee', 'team member', 'assignee']
    
    for name in priority_names:
        for col in columns:
            if name in col.lower():
                return col
    
    return None


def find_date_column(columns: list[str]) -> Optional[str]:
    """Find the date column."""
    priority_names = ['date', 'entry date', 'timestamp', 'created', 'logged']
    
    for name in priority_names:
        for col in columns:
            if name in col.lower():
                return col
    
    return None


def get_date_range(rows: list[dict], date_col: str) -> tuple[Optional[str], Optional[str]]:
    """Extract earliest and latest dates from data."""
    dates = []
    for row in rows:
        date_str = row.get(date_col, '')
        if date_str:
            dates.append(date_str)
    
    if not dates:
        return None, None
    
    # Sort dates (works for YYYY-MM-DD format)
    sorted_dates = sorted(dates)
    return sorted_dates[0], sorted_dates[-1]


def extract_document_info(rows: list[dict], columns: list[str], source_name: str = "") -> dict:
    """Extract metadata about the document for summary."""
    info = {
        "total_rows": len(rows),
        "columns": columns,
        "source": source_name,
    }
    
    # Get date range if available
    date_col = find_date_column(columns)
    if date_col:
        earliest, latest = get_date_range(rows, date_col)
        if earliest and latest:
            info["date_range"] = f"{earliest} to {latest}"
            info["date_column"] = date_col
    
    # Get unique contributors if available
    groupby_col = find_groupby_column(columns)
    if groupby_col:
        contributors = set()
        for row in rows:
            name = row.get(groupby_col, '').strip()
            if name:
                contributors.add(name)
        info["contributors"] = sorted(contributors)
        info["contributor_count"] = len(contributors)
        info["contributor_column"] = groupby_col
    
    return info


def format_user_friendly_result(
    calc_type: str,
    numeric_col: str,
    values: list[float],
    rows: list[dict],
    columns: list[str],
    source_name: str = "",
    grouped_data: dict = None
) -> str:
    """Format calculation results in a user-friendly way with document context."""
    
    doc_info = extract_document_info(rows, columns, source_name)
    result_lines = []
    
    # Document header
    friendly_name = source_name.replace('_', ' ').replace('.csv', '').replace('.xlsx', '').strip() if source_name else "Uploaded Document"
    result_lines.append(f"## 📊 {friendly_name}")
    result_lines.append("")
    
    # Document overview
    result_lines.append("### Document Overview")
    result_lines.append(f"- **Total entries:** {doc_info['total_rows']} rows")
    
    if doc_info.get('contributor_count'):
        result_lines.append(f"- **Team members:** {doc_info['contributor_count']} contributors")
    
    if doc_info.get('date_range'):
        result_lines.append(f"- **Date range:** {doc_info['date_range']}")
    
    result_lines.append("")
    
    # Main calculation result
    if calc_type == "sum":
        total = sum(values)
        avg = total / len(values) if values else 0
        
        result_lines.append("### ⏱️ Total Hours Summary")
        result_lines.append("")
        result_lines.append(f"| Metric | Value |")
        result_lines.append(f"|--------|------:|")
        result_lines.append(f"| **Grand Total** | **{total:,.2f} hours** |")
        result_lines.append(f"| Average per entry | {avg:,.2f} hours |")
        result_lines.append(f"| Number of entries | {len(values)} |")
        
        # Add breakdown by contributor if available
        groupby_col = find_groupby_column(columns)
        if groupby_col:
            by_group = defaultdict(float)
            for row in rows:
                try:
                    group = row.get(groupby_col, 'Unknown').strip()
                    val = float(row.get(numeric_col, 0))
                    by_group[group] += val
                except (ValueError, TypeError):
                    continue
            
            if by_group:
                sorted_groups = sorted(by_group.items(), key=lambda x: -x[1])
                result_lines.append("")
                result_lines.append("### 👥 Breakdown by Contributor")
                result_lines.append("")
                result_lines.append(f"| Team Member | Hours | % of Total |")
                result_lines.append(f"|-------------|------:|----------:|")
                for name, hours in sorted_groups:
                    pct = (hours / total * 100) if total > 0 else 0
                    result_lines.append(f"| {name} | {hours:,.2f} | {pct:.1f}% |")
                result_lines.append(f"| **Total** | **{total:,.2f}** | **100%** |")
        
    elif calc_type == "average":
        total = sum(values)
        avg = total / len(values) if values else 0
        
        result_lines.append("### 📈 Average Analysis")
        result_lines.append("")
        result_lines.append(f"| Metric | Value |")
        result_lines.append(f"|--------|------:|")
        result_lines.append(f"| **Average {numeric_col}** | **{avg:,.2f}** |")
        result_lines.append(f"| Total sum | {total:,.2f} |")
        result_lines.append(f"| Number of entries | {len(values)} |")
        result_lines.append(f"| Minimum | {min(values):,.2f} |")
        result_lines.append(f"| Maximum | {max(values):,.2f} |")
        
    elif calc_type == "count":
        result_lines.append("### 🔢 Count Summary")
        result_lines.append("")
        result_lines.append(f"**Total Entries: {len(rows)}**")
        result_lines.append(f"- Entries with valid {numeric_col}: {len(values)}")
        
    elif calc_type == "group_sum" and grouped_data:
        grand_total = sum(grouped_data.values())
        sorted_groups = sorted(grouped_data.items(), key=lambda x: -x[1])
        
        result_lines.append("### 👥 Hours by Team Member")
        result_lines.append("")
        result_lines.append(f"| Team Member | Hours | % of Total |")
        result_lines.append(f"|-------------|------:|----------:|")
        for name, hours in sorted_groups:
            pct = (hours / grand_total * 100) if grand_total > 0 else 0
            result_lines.append(f"| {name} | {hours:,.2f} | {pct:.1f}% |")
        result_lines.append(f"| **Grand Total** | **{grand_total:,.2f}** | **100%** |")
        
        # Top contributor highlight
        if sorted_groups:
            top_name, top_hours = sorted_groups[0]
            result_lines.append("")
            result_lines.append(f"🏆 **Top contributor:** {top_name} with {top_hours:,.2f} hours ({top_hours/grand_total*100:.1f}% of total)")
        
    elif calc_type == "minmax":
        min_val = min(values)
        max_val = max(values)
        
        result_lines.append("### 📊 Range Analysis")
        result_lines.append("")
        result_lines.append(f"| Metric | Value |")
        result_lines.append(f"|--------|------:|")
        result_lines.append(f"| **Maximum** | **{max_val:,.2f}** |")
        result_lines.append(f"| **Minimum** | **{min_val:,.2f}** |")
        result_lines.append(f"| Range | {max_val - min_val:,.2f} |")
        result_lines.append(f"| Average | {sum(values)/len(values):,.2f} |")
    
    return "\n".join(result_lines)


def calculate_on_data(content: str, calc_type: str, user_text: str = "", source_name: str = "") -> Optional[str]:
    """
    Perform actual calculation on CSV data.
    Returns formatted result string or None if calculation failed.
    """
    rows, columns = parse_csv_content(content)
    
    if not rows or not columns:
        logger.warning("No data parsed from content")
        return None
    
    logger.info(f"Parsed {len(rows)} rows with columns: {columns}")
    
    # Find numeric column
    numeric_col = find_numeric_column(columns, user_text)
    if not numeric_col:
        # Try to find any numeric column by checking first row
        for col in columns:
            try:
                val = rows[0].get(col, '')
                float(val)
                numeric_col = col
                break
            except (ValueError, TypeError):
                continue
    
    if not numeric_col:
        logger.warning("No numeric column found")
        return None
    
    logger.info(f"Using numeric column: {numeric_col}")
    
    # Extract numeric values
    values = []
    for row in rows:
        try:
            val = row.get(numeric_col, '')
            if val:
                values.append(float(val))
        except (ValueError, TypeError):
            continue
    
    if not values:
        logger.warning("No numeric values found")
        return None
    
    # For group_sum, prepare grouped data
    grouped_data = None
    if calc_type == "group_sum":
        groupby_col = find_groupby_column(columns)
        if groupby_col:
            grouped_data = defaultdict(float)
            for row in rows:
                try:
                    group = row.get(groupby_col, 'Unknown').strip()
                    val = float(row.get(numeric_col, 0))
                    grouped_data[group] += val
                except (ValueError, TypeError):
                    continue
            grouped_data = dict(grouped_data)
    
    # Use the user-friendly formatter
    return format_user_friendly_result(
        calc_type=calc_type,
        numeric_col=numeric_col,
        values=values,
        rows=rows,
        columns=columns,
        source_name=source_name,
        grouped_data=grouped_data
    )


def process_calculation_request(user_text: str, attachment_content: str, source_name: str = "") -> Optional[str]:
    """
    Main entry point: detect if calculation is needed and perform it.
    Returns calculation result or None if not a calculation request.
    """
    is_calc, calc_type = detect_calculation_intent(user_text)
    
    if not is_calc:
        logger.debug("No calculation intent detected")
        return None
    
    logger.info(f"Calculation intent detected: {calc_type}")
    
    # Try to calculate
    result = calculate_on_data(attachment_content, calc_type, user_text, source_name)
    
    if result:
        logger.info("Calculation completed successfully")
        return result
    
    logger.warning("Calculation failed, falling back to LLM")
    return None


def _extract_project_name(file_names: list[str]) -> str:
    """
    Extract a common project name from multiple file names.
    E.g., ['Dallas_County_Protective_Order_M1_Hours.csv', 'Dallas_County_Protective_Order_M2_Hours.csv']
    -> 'Dallas County Protective Order'
    """
    if not file_names:
        return ""
    
    # Clean file names - remove extensions and common suffixes
    cleaned = []
    for name in file_names:
        # Remove extension
        name = re.sub(r'\.(csv|xlsx?|txt)$', '', name, flags=re.IGNORECASE)
        # Remove trailing numbers, version markers, "Hours" suffix, and milestone markers
        name = re.sub(r'[\s_]*(Hours?|M\d+|v\d+|\d+)[\s_]*$', '', name, flags=re.IGNORECASE)
        name = re.sub(r'[\s_]+\d+$', '', name)  # Remove trailing numbers
        name = re.sub(r'[\s_]*(Hours?)[\s_]*$', '', name, flags=re.IGNORECASE)  # Remove Hours again
        cleaned.append(name)
    
    if not cleaned:
        return ""
    
    # Find common prefix among all cleaned names
    base = cleaned[0]
    for name in cleaned[1:]:
        # Find common prefix
        common = ""
        for i, (c1, c2) in enumerate(zip(base, name)):
            if c1 == c2:
                common += c1
            else:
                break
        base = common
    
    # Clean up the result - remove trailing M, underscores, dashes, spaces
    result = base.rstrip('_- ')
    result = re.sub(r'[\s_-]+M$', '', result)  # Remove trailing " M" or "_M" or "-M"
    result = result.rstrip('_- ')
    # Replace underscores with spaces
    result = result.replace('_', ' ').replace('-', ' ')
    # Remove duplicate spaces
    result = re.sub(r'\s+', ' ', result).strip()
    
    if len(result) < 5:  # Too short, probably not meaningful
        return ""
    
    return result


def process_multi_file_calculation(user_text: str, files: list[dict]) -> Optional[str]:
    """
    Process calculation across multiple uploaded files.
    files: list of {"name": str, "content": str}
    Returns combined summary or None.
    """
    is_calc, calc_type = detect_calculation_intent(user_text)
    
    if not is_calc:
        logger.debug("No calculation intent detected for multi-file")
        return None
    
    if not files or len(files) < 2:
        logger.debug("Less than 2 files, using single-file calculation")
        return None
    
    logger.info(f"Multi-file calculation: {len(files)} files, calc_type={calc_type}")
    
    # Parse all files
    file_data = []
    for f in files:
        name = f.get("name", "Unknown")
        content = f.get("content", "")
        if not content:
            continue
        
        rows, columns = parse_csv_content(content)
        if rows and columns:
            file_data.append({
                "name": name,
                "rows": rows,
                "columns": columns,
                "content": content
            })
    
    if len(file_data) < 2:
        logger.warning("Could not parse at least 2 files")
        return None
    
    # Extract a common project name from file names for a nice heading
    file_names = [fd["name"] for fd in file_data]
    project_name = _extract_project_name(file_names)
    
    # Build combined summary
    result_lines = []
    if project_name:
        result_lines.append(f"# 📊 {project_name} - Hours Summary")
    else:
        result_lines.append("# 📊 Combined Hours Summary")
    result_lines.append("")
    result_lines.append(f"Analyzed **{len(file_data)} files** for total hours across all participants.")
    result_lines.append("")
    
    # Collect stats from each file
    all_contributors = defaultdict(float)
    file_summaries = []
    grand_total = 0
    total_rows = 0
    
    for fd in file_data:
        name = fd["name"]
        rows = fd["rows"]
        columns = fd["columns"]
        
        # Find numeric and groupby columns
        numeric_col = find_numeric_column(columns, user_text)
        groupby_col = find_groupby_column(columns)
        date_col = find_date_column(columns)
        
        if not numeric_col:
            for col in columns:
                try:
                    val = rows[0].get(col, '')
                    float(val)
                    numeric_col = col
                    break
                except (ValueError, TypeError):
                    continue
        
        if not numeric_col:
            continue
        
        # Calculate file totals
        file_total = 0
        file_contributors = defaultdict(float)
        for row in rows:
            try:
                val = float(row.get(numeric_col, 0))
                file_total += val
                
                if groupby_col:
                    person = row.get(groupby_col, 'Unknown').strip()
                    file_contributors[person] += val
                    all_contributors[person] += val
            except (ValueError, TypeError):
                continue
        
        # Get date range
        date_range = ""
        if date_col:
            earliest, latest = get_date_range(rows, date_col)
            if earliest and latest:
                date_range = f"{earliest} to {latest}"
        
        friendly_name = name.replace('_', ' ').replace('.csv', '').replace('.xlsx', '').strip()
        file_summaries.append({
            "name": friendly_name,
            "original_name": name,
            "total": file_total,
            "rows": len(rows),
            "contributors": dict(file_contributors),
            "contributor_count": len(file_contributors),
            "date_range": date_range
        })
        
        grand_total += file_total
        total_rows += len(rows)
    
    if not file_summaries:
        return None
    
    # Overall summary table
    result_lines.append("## 📈 Overall Summary")
    result_lines.append("")
    result_lines.append("| Metric | Value |")
    result_lines.append("|--------|------:|")
    result_lines.append(f"| **Combined Grand Total** | **{grand_total:,.2f} hours** |")
    result_lines.append(f"| Total entries across files | {total_rows} |")
    result_lines.append(f"| Files analyzed | {len(file_summaries)} |")
    result_lines.append(f"| Unique contributors | {len(all_contributors)} |")
    result_lines.append("")
    
    # Per-file breakdown
    result_lines.append("## 📁 Hours by File")
    result_lines.append("")
    result_lines.append("| File | Hours | Entries | Contributors |")
    result_lines.append("|------|------:|--------:|-------------:|")
    
    for fs in sorted(file_summaries, key=lambda x: -x["total"]):
        result_lines.append(f"| {fs['name'][:40]} | {fs['total']:,.2f} | {fs['rows']} | {fs['contributor_count']} |")
    result_lines.append(f"| **Combined Total** | **{grand_total:,.2f}** | **{total_rows}** | **{len(all_contributors)}** |")
    result_lines.append("")
    
    # Contributor breakdown across all files
    if all_contributors:
        result_lines.append("## 👥 Total Hours by Contributor (All Files)")
        result_lines.append("")
        result_lines.append("| Team Member | Total Hours | % of Grand Total |")
        result_lines.append("|-------------|------------:|-----------------:|")
        
        sorted_contributors = sorted(all_contributors.items(), key=lambda x: -x[1])
        for person, hours in sorted_contributors:
            pct = (hours / grand_total * 100) if grand_total > 0 else 0
            result_lines.append(f"| {person} | {hours:,.2f} | {pct:.1f}% |")
        result_lines.append(f"| **Grand Total** | **{grand_total:,.2f}** | **100%** |")
        result_lines.append("")
        
        # Top contributor
        if sorted_contributors:
            top_name, top_hours = sorted_contributors[0]
            result_lines.append(f"🏆 **Top contributor overall:** {top_name} with **{top_hours:,.2f} hours** ({top_hours/grand_total*100:.1f}% of total)")
            result_lines.append("")
    
    # Individual file details
    result_lines.append("---")
    result_lines.append("")
    result_lines.append("## 📋 Detailed File Breakdown")
    result_lines.append("")
    
    for i, fs in enumerate(file_summaries, 1):
        result_lines.append(f"### {i}. {fs['name']}")
        result_lines.append(f"- **Total hours:** {fs['total']:,.2f}")
        result_lines.append(f"- **Entries:** {fs['rows']}")
        if fs['date_range']:
            result_lines.append(f"- **Date range:** {fs['date_range']}")
        
        if fs['contributors']:
            result_lines.append(f"- **Contributors:** {', '.join(sorted(fs['contributors'].keys()))}")
        result_lines.append("")
    
    return "\n".join(result_lines)
