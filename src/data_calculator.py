"""
Data Calculator Module
Performs accurate calculations on CSV/Excel data using pandas and Python code.
This overcomes LLM arithmetic limitations by executing real calculations.
Supports CSV, XLSX, and XLS files with comprehensive numeric analysis.
"""

import csv
import io
import re
import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional, Union
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


MEASURE_KEYWORDS = (
    "hours", "amount", "total", "value", "cost", "quantity", "qty", "price",
    "salary", "wage", "revenue", "sales", "budget", "estimate", "estimated",
    "actual", "balance", "rate", "duration", "days",
)

IDENTIFIER_KEYWORDS = (
    "id", "key", "code", "number", "no", "num", "identifier", "uuid", "guid",
)


def _norm_col(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def is_identifier_column(col: str, series: pd.Series = None) -> bool:
    """Return True for numeric-looking columns that are labels, not measures."""
    norm = _norm_col(col)
    tokens = set(norm.split())
    compact = norm.replace(" ", "")

    if compact.endswith("id") or "id" in tokens:
        return True
    if any(token in tokens for token in IDENTIFIER_KEYWORDS):
        return True
    if any(word in compact for word in ("uuid", "guid")):
        return True

    if series is not None:
        non_null = series.dropna()
        if len(non_null) > 0:
            unique_ratio = non_null.nunique() / len(non_null)
            numeric = pd.to_numeric(non_null, errors="coerce")
            looks_integral = numeric.notna().all() and (numeric.dropna() % 1 == 0).all()
            if looks_integral and unique_ratio > 0.9 and not any(k in norm for k in MEASURE_KEYWORDS):
                return True

    return False


def measure_columns(df: pd.DataFrame) -> list[str]:
    """Numeric columns that are appropriate for sums/averages/min/max."""
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    return [col for col in numeric_cols if not is_identifier_column(col, df[col])]


def categorical_columns(df: pd.DataFrame, max_unique_ratio: float = 0.85) -> list[str]:
    """Columns suitable for counts/value-counts/grouping."""
    cols = []
    for col in df.columns:
        if is_identifier_column(col, df[col] if col in df else None):
            continue
        non_null = df[col].dropna()
        if len(non_null) == 0:
            continue
        unique_count = non_null.nunique()
        unique_ratio = unique_count / len(non_null)
        if not pd.api.types.is_numeric_dtype(df[col]) or unique_ratio <= max_unique_ratio:
            cols.append(col)
    return cols


def _matching_column(df: pd.DataFrame, user_text: str, candidates: list[str] = None) -> Optional[str]:
    """Find a column explicitly mentioned by the user."""
    text = _norm_col(user_text)
    search_cols = candidates or list(df.columns)
    for col in search_cols:
        col_norm = _norm_col(col)
        if col_norm and re.search(rf"\b{re.escape(col_norm)}\b", text):
            return col
        compact = col_norm.replace(" ", "")
        if compact and compact in text.replace(" ", ""):
            return col
    return None


def identify_calculation_plan(df: pd.DataFrame, calc_type: str, user_text: str = "") -> dict:
    """Choose the operation and columns before pandas performs arithmetic.

    This prevents accidental math on identifiers such as ProjectID and steers
    categorical requests like "contract type breakdown" to value counts.
    """
    text = (user_text or "").lower()
    measures = measure_columns(df)
    categories = categorical_columns(df)
    mentioned_measure = _matching_column(df, user_text, measures)
    mentioned_category = _matching_column(df, user_text, categories)

    plan = {
        "operation": calc_type,
        "value_column": None,
        "groupby_column": None,
        "reason": "",
        "measure_columns": measures,
        "categorical_columns": categories,
    }

    breakdown_requested = bool(re.search(r"\b(breakdown|by|per|group(?:ed)?|distribution|status(?:es)?|types?)\b", text))
    count_requested = calc_type == "count" or bool(re.search(r"\b(count|how many|number of|frequency|distribution|breakdown)\b", text))

    if count_requested or (breakdown_requested and mentioned_category):
        plan["operation"] = "value_counts" if (mentioned_category or breakdown_requested) else "count_rows"
        plan["groupby_column"] = mentioned_category
        plan["reason"] = "categorical count requested"
        return plan

    if calc_type in {"sum", "average", "max", "min", "median", "rank", "percent", "difference"}:
        plan["value_column"] = mentioned_measure or find_numeric_column_pandas(df, user_text)
        plan["groupby_column"] = mentioned_category if breakdown_requested else None
        plan["reason"] = "measure aggregation requested"
        return plan

    if calc_type == "group_sum":
        plan["value_column"] = mentioned_measure or find_numeric_column_pandas(df, user_text)
        plan["groupby_column"] = mentioned_category or find_groupby_column(list(df.columns))
        plan["reason"] = "grouped measure aggregation requested"
        return plan

    plan["value_column"] = mentioned_measure or find_numeric_column_pandas(df, user_text)
    plan["groupby_column"] = mentioned_category
    plan["reason"] = "default calculation plan"
    return plan


def _format_value_counts(df: pd.DataFrame, groupby_col: str, source_name: str = "") -> str:
    friendly_name = source_name.replace('_', ' ').replace('.csv', '').replace('.xlsx', '').strip() if source_name else "Uploaded Document"
    series = df[groupby_col]
    counts = series.fillna("Missing").replace("", "Missing").value_counts(dropna=False)
    total = int(counts.sum())
    lines = [
        f"## Data Count: {friendly_name}",
        "",
        f"Counted **{total:,} row(s)** by **{groupby_col}**.",
        "",
        f"| {groupby_col} | Count |",
        "|---|---:|",
    ]
    for value, count in counts.items():
        label = str(value) if str(value).strip() else "Missing"
        lines.append(f"| {label} | {int(count):,} |")
    lines.append(f"| **Total** | **{total:,}** |")
    return "\n".join(lines)


def extract_unique_categorical_values(df: pd.DataFrame, max_unique: int = 100) -> dict:
    """
    Extract unique values from all categorical/text columns for verification.
    This helps prevent hallucination by explicitly listing actual values.
    
    Args:
        df: pandas DataFrame to analyze
        max_unique: Maximum unique values to report per column
    
    Returns:
        Dictionary mapping column name to list of unique values
    """
    categorical_values = {}
    
    for col in df.columns:
        # Only process text/categorical columns
        if not pd.api.types.is_numeric_dtype(df[col]) and not pd.api.types.is_datetime64_any_dtype(df[col]):
            unique_vals = df[col].dropna().unique()
            if len(unique_vals) <= max_unique:  # Only store if reasonable number
                categorical_values[col] = sorted([str(v) for v in unique_vals])
    
    return categorical_values


def verify_names_in_data(names: list[str], df: pd.DataFrame, name_column: str = None) -> tuple[list[str], list[str]]:
    """
    Verify that names exist in the actual data.
    Returns (valid_names, invalid_names)
    
    Args:
        names: List of names to verify
        df: pandas DataFrame containing the data
        name_column: Name of the column containing names (auto-detected if None)
    
    Returns:
        Tuple of (valid_names, invalid_names)
    """
    if name_column is None:
        # Auto-detect name column
        name_column = find_groupby_column(list(df.columns))
    
    if name_column is None or name_column not in df.columns:
        return [], names  # Cannot verify
    
    actual_names = set(df[name_column].dropna().unique())
    valid = [n for n in names if n in actual_names]
    invalid = [n for n in names if n not in actual_names]
    
    return valid, invalid


def detect_calculation_intent(user_text: str) -> tuple[bool, str]:
    """Detect when a user is asking for deterministic data analysis.

    The old version only routed very explicit "grand total across files" requests
    to pandas. That let the LLM answer spreadsheet questions from snippets, which
    causes incorrect totals, rankings, averages, min/max values, and counts.

    This function intentionally routes common spreadsheet/CSV analytical questions
    to the calculator layer while leaving normal summaries and document questions
    to the LLM.
    """
    text = (user_text or "").lower().strip()
    if not text:
        return False, ""

    # Avoid false positives for normal document summarization/review prompts.
    non_calc_patterns = [
        r"\b(summarize|summary|review|explain|rewrite|draft|compose|format)\b",
        r"\b(policy|procedure|guideline|contract|agreement|case study)\b",
    ]
    if any(re.search(pattern, text) for pattern in non_calc_patterns) and not re.search(
        r"\b(total|sum|average|avg|mean|count|how many|highest|lowest|max|min|most|least|rank|top|bottom|percent|percentage|difference|variance|median|breakdown|distribution|frequency)\b",
        text,
    ):
        return False, ""

    calculation_patterns = [
        ("sum", r"\b(sum|total|grand total|add up|combined total)\b"),
        ("average", r"\b(average|avg|mean)\b"),
        ("count", r"\b(count|how many|number of|frequency)\b"),
        ("max", r"\b(highest|max|maximum|largest|most)\b"),
        ("min", r"\b(lowest|min|minimum|smallest|least)\b"),
        ("rank", r"\b(rank|ranking|top\s+\d+|bottom\s+\d+|sort by)\b"),
        ("percent", r"\b(percent|percentage|rate|ratio|share)\b"),
        ("difference", r"\b(difference|change|variance|gap|compare totals?)\b"),
        ("median", r"\b(median|middle value)\b"),
        ("count", r"\b(breakdown|distribution|by status|by type|rag status|contract type|status count)\b"),
    ]

    data_context = re.search(
        r"\b(file|files|spreadsheet|excel|xlsx|xls|csv|table|data|dataset|rows|columns|sheet|report|amount|sales|hours|cost|revenue|price|quantity)\b",
        text,
    )
    category_context = re.search(
        r"\b(contract type|rag status|status|type|phase|client|customer|category|breakdown|distribution)\b",
        text,
    )

    for calc_type, pattern in calculation_patterns:
        if re.search(pattern, text) and (data_context or category_context):
            return True, calc_type

    # Natural phrasing like "who has the most hours" or "which state has highest sales".
    if re.search(r"\b(who|which|what)\b.*\b(most|least|highest|lowest|largest|smallest)\b", text):
        return True, "rank"

    return False, ""


def _extract_embedded_table_text(content: str) -> tuple[str, Optional[str]]:
    """Extract raw CSV/TSV data from attachment summaries or Excel text output."""
    text = (content or "").strip()
    if not text:
        return text, None

    fenced = re.search(r"```(?:csv|tsv)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if fenced:
        table = fenced.group(1).strip()
        delimiter = "\t" if "\t" in table.splitlines()[0] else ","
        return table, delimiter

    marker_match = re.search(r"(?:FULL CSV DATA|RAW CSV DATA|CALCULATION DATA)\s*:?", text, re.IGNORECASE)
    if marker_match:
        table = text[marker_match.end():].strip()
        table = re.sub(r"^```(?:csv|tsv)?\s*", "", table, flags=re.IGNORECASE).strip()
        table = re.sub(r"```\s*$", "", table).strip()
        delimiter = "\t" if table.splitlines() and "\t" in table.splitlines()[0] else ","
        return table, delimiter

    lines = text.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        if stripped.lower().startswith("==="):
            continue
        if "," in stripped or "\t" in stripped:
            table = "\n".join(lines[idx:]).strip()
            delimiter = "\t" if "\t" in stripped else ","
            return table, delimiter

    return text, None


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


def load_data_with_pandas(content: str, file_name: str = "") -> Optional[pd.DataFrame]:
    """
    Load data using pandas for robust CSV and Excel support.
    Automatically detects file type from content or file name.
    
    Args:
        content: File content (string for CSV, bytes for Excel)
        file_name: Original file name to detect type
    
    Returns:
        pandas DataFrame or None if loading failed
    """
    try:
        # Detect file type from name
        is_excel = False
        if file_name:
            file_lower = file_name.lower()
            is_excel = file_lower.endswith(('.xlsx', '.xls'))
        
        if is_excel and not isinstance(content, str):
            # Excel file - content should be bytes
            if isinstance(content, str):
                # If content is string, it might be base64 or we need to re-read
                logger.warning("Excel content is string, attempting CSV parse instead")
                df = pd.read_csv(io.StringIO(content))
            else:
                # Read Excel directly from bytes
                df = pd.read_excel(io.BytesIO(content), engine='openpyxl')
            logger.info(f"Loaded Excel file with pandas: {len(df)} rows, {len(df.columns)} columns")
        else:
            # CSV/TSV text, including extracted Excel text and attachment summaries.
            if isinstance(content, bytes):
                content = content.decode('utf-8')
            content, detected_delimiter = _extract_embedded_table_text(content)
            
            # Try different CSV delimiters
            try:
                if detected_delimiter:
                    df = pd.read_csv(io.StringIO(content), sep=detected_delimiter)
                else:
                    df = pd.read_csv(io.StringIO(content))
            except Exception:
                # Try semicolon delimiter
                try:
                    df = pd.read_csv(io.StringIO(content), sep=';')
                except Exception:
                    df = pd.read_csv(io.StringIO(content), sep='\t')
            
            logger.info(f"Loaded CSV file with pandas: {len(df)} rows, {len(df.columns)} columns")
        
        # Clean column names - strip whitespace
        df.columns = df.columns.str.strip()
        
        return df
        
    except Exception as e:
        logger.error(f"Failed to load data with pandas: {e}")
        return None


def analyze_dataframe(df: pd.DataFrame, user_text: str = "") -> dict:
    """
    Perform comprehensive analysis on pandas DataFrame.
    
    Args:
        df: pandas DataFrame to analyze
        user_text: User's question for context hints
    
    Returns:
        Dictionary with analysis results
    """
    analysis = {
        "total_rows": len(df),
        "total_columns": len(df.columns),
        "columns": list(df.columns),
        "numeric_columns": [],
        "date_columns": [],
        "text_columns": [],
        "statistics": {}
    }
    
    # Identify column types. Numeric identifiers are tracked separately so the
    # model does not treat ProjectID/EmployeeID/etc. as meaningful totals.
    for col in df.columns:
        dtype = df[col].dtype
        
        if pd.api.types.is_numeric_dtype(dtype):
            if is_identifier_column(col, df[col]):
                analysis["text_columns"].append(col)
                analysis["statistics"][col] = {
                    "unique_count": int(df[col].nunique()),
                    "identifier": True,
                    "non_null_count": int(df[col].count()),
                }
            else:
                analysis["numeric_columns"].append(col)
                analysis["statistics"][col] = {
                    "sum": float(df[col].sum()),
                    "mean": float(df[col].mean()),
                    "median": float(df[col].median()),
                    "min": float(df[col].min()),
                    "max": float(df[col].max()),
                    "std": float(df[col].std()),
                    "count": int(df[col].count()),
                    "null_count": int(df[col].isnull().sum())
                }
        elif pd.api.types.is_datetime64_any_dtype(dtype) or 'date' in col.lower():
            analysis["date_columns"].append(col)
            # Try to parse as datetime if not already
            try:
                date_series = pd.to_datetime(df[col], errors='coerce')
                valid_dates = date_series.dropna()
                if len(valid_dates) > 0:
                    analysis["statistics"][col] = {
                        "earliest": str(valid_dates.min()),
                        "latest": str(valid_dates.max()),
                        "count": int(len(valid_dates))
                    }
            except Exception:
                pass
        else:
            analysis["text_columns"].append(col)
            # For text columns, count unique values
            unique_count = df[col].nunique()
            if unique_count <= 50:  # Only for categorical-like columns
                analysis["statistics"][col] = {
                    "unique_count": int(unique_count),
                    "top_values": df[col].value_counts().head(10).to_dict()
                }
    
    logger.info(f"DataFrame analysis: {len(analysis['numeric_columns'])} numeric, "
                f"{len(analysis['date_columns'])} date, {len(analysis['text_columns'])} text columns")
    
    return analysis


def find_numeric_column_pandas(df: pd.DataFrame, hint: str = "") -> Optional[str]:
    """Find the most likely numeric column using pandas dtype detection."""
    hint_lower = hint.lower()
    
    numeric_cols = measure_columns(df)
    
    if not numeric_cols:
        return None
    
    # Priority keywords for column selection
    priority_names = ['hours', 'amount', 'total', 'value', 'cost', 'quantity', 'count', 'price', 'salary', 'wage', 'revenue', 'sales']
    
    # First, check if hint matches a numeric column
    for col in numeric_cols:
        if hint_lower and hint_lower in col.lower():
            return col
    
    # Then check priority names
    for name in priority_names:
        for col in numeric_cols:
            if name in col.lower():
                return col
    
    # Return first numeric column
    return numeric_cols[0] if numeric_cols else None


def find_numeric_column(columns: list[str], hint: str = "") -> Optional[str]:
    """Find the most likely numeric column for calculations."""
    hint_lower = hint.lower()
    
    # Priority columns based on common naming
    priority_names = ['hours', 'amount', 'total', 'value', 'cost', 'quantity', 'qty', 'price', 'salary', 'wage', 'revenue', 'sales']
    
    # First, check if hint matches a column
    for col in columns:
        if is_identifier_column(col):
            continue
        if hint_lower and hint_lower in col.lower():
            return col
    
    # Then check priority names
    for name in priority_names:
        for col in columns:
            if not is_identifier_column(col) and name in col.lower():
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
    
    # ✅ ANTI-HALLUCINATION: Show unique contributors FIRST if available
    if doc_info.get('contributors'):
        result_lines.append("### 👥 Verified Contributors")
        result_lines.append("")
        result_lines.append(f"**{len(doc_info['contributors'])} team members** found in this file:")
        result_lines.append("")
        result_lines.append(", ".join(doc_info['contributors']))
        result_lines.append("")
        result_lines.append("---")
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
    Perform actual calculation on CSV/Excel data using pandas.
    Returns formatted result string or None if calculation failed.
    
    Enhanced with pandas for:
    - CSV and Excel (.xlsx, .xls) support
    - Automatic numeric column detection
    - Comprehensive statistical analysis
    - Advanced grouping and aggregation
    """
    # Try pandas first for robust CSV and Excel support
    df = load_data_with_pandas(content, source_name)
    
    if df is not None and not df.empty:
        # Use pandas-based analysis
        try:
            analysis = analyze_dataframe(df, user_text)

            plan = identify_calculation_plan(df, calc_type, user_text)
            logger.info(
                "Calculation plan: operation=%s value=%s groupby=%s reason=%s",
                plan.get("operation"),
                plan.get("value_column"),
                plan.get("groupby_column"),
                plan.get("reason"),
            )

            if plan.get("operation") == "count_rows":
                return (
                    f"## Data Count: {source_name or 'Uploaded Document'}\n\n"
                    f"**Total rows/projects:** {len(df):,}"
                )

            if plan.get("operation") == "value_counts" and plan.get("groupby_column"):
                return _format_value_counts(df, plan["groupby_column"], source_name)

            numeric_col = plan.get("value_column")
            if not numeric_col:
                logger.warning("No numeric column found in DataFrame")
                # Fall back to legacy CSV parsing
                df = None
            else:
                logger.info(f"Using numeric column with pandas: {numeric_col}")
                
                # Get numeric values (drop NaN)
                values = df[numeric_col].dropna().tolist()
                
                if not values:
                    logger.warning("No valid numeric values found")
                    return None
                
                # Find groupby column for aggregation
                groupby_col = plan.get("groupby_column") or find_groupby_column(analysis["columns"])
                
                # Prepare grouped data if needed
                grouped_data = None
                if calc_type == "group_sum" and groupby_col:
                    grouped_data = df.groupby(groupby_col)[numeric_col].sum().to_dict()
                
                # Convert DataFrame to legacy format for formatting function
                rows = df.to_dict('records')
                columns = list(df.columns)
                
                # Use existing formatter
                return format_user_friendly_result(
                    calc_type=calc_type,
                    numeric_col=numeric_col,
                    values=values,
                    rows=rows,
                    columns=columns,
                    source_name=source_name,
                    grouped_data=grouped_data
                )
        except Exception as e:
            logger.error(f"Pandas calculation failed: {e}, falling back to CSV parsing")
            df = None
    
    # Fall back to legacy CSV parsing if pandas fails
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
            if is_identifier_column(col):
                continue
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
    Process calculation across multiple uploaded files using pandas.
    files: list of {"name": str, "content": str}
    Returns combined summary or None.
    
    Enhanced to support both CSV and Excel files with pandas.
    """
    is_calc, calc_type = detect_calculation_intent(user_text)
    
    if not is_calc:
        logger.debug("No calculation intent detected for multi-file")
        return None
    
    if not files or len(files) < 2:
        logger.debug("Less than 2 files, using single-file calculation")
        return None
    
    logger.info(f"Multi-file calculation: {len(files)} files, calc_type={calc_type}")
    
    # Parse all files with pandas first, fall back to CSV parsing
    file_data = []
    for f in files:
        name = f.get("name", "Unknown")
        content = f.get("content", "")
        if not content:
            continue
        
        # Try pandas first
        df = load_data_with_pandas(content, name)
        if df is not None and not df.empty:
            rows = df.to_dict('records')
            columns = list(df.columns)
            file_data.append({
                "name": name,
                "rows": rows,
                "columns": columns,
                "content": content,
                "dataframe": df
            })
        else:
            # Fall back to CSV parsing
            rows, columns = parse_csv_content(content)
            if rows and columns:
                file_data.append({
                    "name": name,
                    "rows": rows,
                    "columns": columns,
                    "content": content,
                    "dataframe": None
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
    all_contributors_by_file = {}  # Track which file each contributor appears in
    file_summaries = []
    grand_total = 0
    total_rows = 0
    
    for fd in file_data:
        name = fd["name"]
        rows = fd["rows"]
        columns = fd["columns"]
        df = fd.get("dataframe")
        
        # Find numeric and groupby columns
        numeric_col = find_numeric_column(columns, user_text)
        groupby_col = find_groupby_column(columns)
        date_col = find_date_column(columns)
        
        if not numeric_col:
            for col in columns:
                if is_identifier_column(col, df[col] if df is not None and col in df.columns else None):
                    continue
                try:
                    val = rows[0].get(col, '')
                    float(val)
                    numeric_col = col
                    break
                except (ValueError, TypeError):
                    continue
        
        if not numeric_col:
            continue
        
        # Extract unique contributors from THIS file for verification
        file_unique_contributors = set()
        if df is not None and groupby_col and groupby_col in df.columns:
            file_unique_contributors = set(df[groupby_col].dropna().unique())
        
        # Calculate file totals
        file_total = 0
        file_contributors = defaultdict(float)
        for row in rows:
            try:
                val = float(row.get(numeric_col, 0))
                file_total += val
                
                if groupby_col:
                    person = row.get(groupby_col, 'Unknown').strip()
                    
                    # VERIFICATION: Only count if person exists in file's unique contributors
                    if file_unique_contributors and person not in file_unique_contributors:
                        logger.warning(f"⚠️ VERIFICATION FAILED: '{person}' not found in {name} actual data")
                        continue
                    
                    file_contributors[person] += val
                    all_contributors[person] += val
                    
                    # Track which files this person appears in
                    if person not in all_contributors_by_file:
                        all_contributors_by_file[person] = []
                    all_contributors_by_file[person].append(name)
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
            "date_range": date_range,
            "unique_contributors": sorted(file_unique_contributors) if file_unique_contributors else []
        })
        
        grand_total += file_total
        total_rows += len(rows)
    
    if not file_summaries:
        return None
    
    # ✅ ANTI-HALLUCINATION: List all unique contributors found across ALL files FIRST
    result_lines.append("## 👥 Verified Contributors")
    result_lines.append("")
    result_lines.append(f"**{len(all_contributors)} unique team members** found across all files:")
    result_lines.append("")
    
    # List per file for transparency
    for fs in file_summaries:
        if fs["unique_contributors"]:
            result_lines.append(f"**{fs['name']}:**")
            result_lines.append(f"  {', '.join(fs['unique_contributors'])} ({len(fs['unique_contributors'])} total)")
            result_lines.append("")
    
    result_lines.append("---")
    result_lines.append("")
    
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


def generate_data_exploration_report(content: str, source_name: str = "") -> Optional[str]:
    """
    Generate a comprehensive data exploration report for CSV/Excel files.
    Uses pandas for advanced statistical analysis.
    
    This function provides:
    - Column types and data quality
    - Statistical summaries for numeric columns
    - Distribution analysis
    - Missing data analysis
    - Correlations between numeric columns
    
    Args:
        content: File content (CSV string or Excel bytes)
        source_name: File name for context
    
    Returns:
        Formatted markdown report or None if analysis failed
    """
    df = load_data_with_pandas(content, source_name)
    
    if df is None or df.empty:
        logger.warning("Could not load data for exploration")
        return None
    
    try:
        analysis = analyze_dataframe(df)
        result_lines = []
        
        # Header
        friendly_name = source_name.replace('_', ' ').replace('.csv', '').replace('.xlsx', '').replace('.xls', '').strip() if source_name else "Data File"
        result_lines.append(f"# 📊 Data Exploration: {friendly_name}")
        result_lines.append("")
        
        # Overview
        result_lines.append("## 📋 Dataset Overview")
        result_lines.append(f"- **Total Rows:** {analysis['total_rows']:,}")
        result_lines.append(f"- **Total Columns:** {analysis['total_columns']}")
        result_lines.append(f"- **Numeric Columns:** {len(analysis['numeric_columns'])}")
        result_lines.append(f"- **Date Columns:** {len(analysis['date_columns'])}")
        result_lines.append(f"- **Text Columns:** {len(analysis['text_columns'])}")
        result_lines.append("")
        
        # Column List
        result_lines.append("## 📝 Columns")
        result_lines.append("| Column Name | Type | Non-Null Count |")
        result_lines.append("|-------------|------|---------------:|")
        for col in df.columns:
            dtype = "Numeric" if col in analysis['numeric_columns'] else ("Date" if col in analysis['date_columns'] else "Text")
            non_null = df[col].count()
            result_lines.append(f"| {col} | {dtype} | {non_null:,} |")
        result_lines.append("")
        
        # Numeric Column Statistics
        if analysis['numeric_columns']:
            result_lines.append("## 📈 Numeric Column Statistics")
            result_lines.append("")
            for col in analysis['numeric_columns']:
                stats = analysis['statistics'].get(col, {})
                result_lines.append(f"### {col}")
                result_lines.append("| Metric | Value |")
                result_lines.append("|--------|------:|")
                result_lines.append(f"| Sum | {stats.get('sum', 0):,.2f} |")
                result_lines.append(f"| Mean | {stats.get('mean', 0):,.2f} |")
                result_lines.append(f"| Median | {stats.get('median', 0):,.2f} |")
                result_lines.append(f"| Std Dev | {stats.get('std', 0):,.2f} |")
                result_lines.append(f"| Min | {stats.get('min', 0):,.2f} |")
                result_lines.append(f"| Max | {stats.get('max', 0):,.2f} |")
                result_lines.append(f"| Count | {stats.get('count', 0):,} |")
                if stats.get('null_count', 0) > 0:
                    result_lines.append(f"| Missing | {stats.get('null_count', 0):,} |")
                result_lines.append("")
        
        # Date Range Analysis
        if analysis['date_columns']:
            result_lines.append("## 📅 Date Range Analysis")
            result_lines.append("")
            for col in analysis['date_columns']:
                stats = analysis['statistics'].get(col, {})
                if stats:
                    result_lines.append(f"### {col}")
                    result_lines.append(f"- **Earliest:** {stats.get('earliest', 'N/A')}")
                    result_lines.append(f"- **Latest:** {stats.get('latest', 'N/A')}")
                    result_lines.append(f"- **Valid Dates:** {stats.get('count', 0):,}")
                    result_lines.append("")
        
        # Categorical/Text Column Analysis
        if analysis['text_columns']:
            result_lines.append("## 🏷️ Categorical Column Summary")
            result_lines.append("")
            for col in analysis['text_columns'][:5]:  # Limit to first 5 text columns
                stats = analysis['statistics'].get(col, {})
                if stats and 'unique_count' in stats:
                    result_lines.append(f"### {col}")
                    result_lines.append(f"- **Unique Values:** {stats['unique_count']}")
                    top_values = stats.get('top_values', {})
                    if top_values:
                        result_lines.append("- **Top Values:**")
                        for value, count in list(top_values.items())[:5]:
                            result_lines.append(f"  - {value}: {count} occurrences")
                    result_lines.append("")
        
        # Missing Data Summary
        missing_summary = []
        for col in df.columns:
            null_count = df[col].isnull().sum()
            if null_count > 0:
                missing_pct = (null_count / len(df)) * 100
                missing_summary.append((col, null_count, missing_pct))
        
        if missing_summary:
            result_lines.append("## ⚠️ Missing Data Analysis")
            result_lines.append("| Column | Missing Count | % Missing |")
            result_lines.append("|--------|--------------|-----------|")
            for col, count, pct in sorted(missing_summary, key=lambda x: -x[1]):
                result_lines.append(f"| {col} | {count:,} | {pct:.1f}% |")
            result_lines.append("")
        
        # Correlation Analysis (for numeric columns)
        if len(analysis['numeric_columns']) >= 2:
            result_lines.append("## 🔗 Correlation Analysis")
            result_lines.append("Correlations between numeric columns:")
            result_lines.append("")
            corr_matrix = df[analysis['numeric_columns']].corr()
            
            # Show only strong correlations (> 0.5 or < -0.5)
            strong_corr = []
            for i, col1 in enumerate(analysis['numeric_columns']):
                for col2 in analysis['numeric_columns'][i+1:]:
                    corr_val = corr_matrix.loc[col1, col2]
                    if abs(corr_val) > 0.5:
                        strong_corr.append((col1, col2, corr_val))
            
            if strong_corr:
                result_lines.append("| Column 1 | Column 2 | Correlation |")
                result_lines.append("|----------|----------|-------------|")
                for col1, col2, corr in sorted(strong_corr, key=lambda x: -abs(x[2])):
                    result_lines.append(f"| {col1} | {col2} | {corr:.3f} |")
            else:
                result_lines.append("*No strong correlations found (threshold: |r| > 0.5)*")
            result_lines.append("")
        
        return "\n".join(result_lines)
        
    except Exception as e:
        logger.error(f"Data exploration failed: {e}", exc_info=True)
        return None
