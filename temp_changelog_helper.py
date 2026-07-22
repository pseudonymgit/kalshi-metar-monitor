#!/usr/bin/env python3
"""
Helper script to add changelog headers to Python files
"""
import os
import subprocess
from datetime import datetime


def get_git_history_for_file(file_path):
    """
    Get Git history for a specific file, excluding auto: weather data updates
    """
    try:
        result = subprocess.run([
            'git', 'log', '--follow', '--pretty=format:%ai %s', '--diff-filter=ACM', file_path
        ], capture_output=True, text=True, cwd=os.getcwd())
        
        if result.returncode != 0:
            print(f"Error getting git history for {file_path}: {result.stderr}")
            return []
        
        commits = result.stdout.strip().split('\n')
        # Filter out auto weather data updates
        filtered_commits = []
        for commit in commits:
            if commit and 'auto: weather data update' not in commit:
                filtered_commits.append(commit)
        
        # Return top 10 commits (if they exist)
        return filtered_commits[:10]
    
    except Exception as e:
        print(f"Exception getting history for {file_path}: {e}")
        return []


def add_changelog_header(file_path):
    """
    Add a changelog header to the file
    """
    # Read current file content
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Get git history for the file
    commits = get_git_history_for_file(file_path)
    
    # If no history, create a default initial implementation entry
    if not commits:
        date_str = datetime.now().strftime('%Y-%m-%d')
        changelog_header = f'''"""
# CHANGELOG (last 10 broad changes):
# 1. [{date_str}] Initial implementation
"""

'''
    else:
        # Format the changelog header
        changelog_entries = []
        for i, commit in enumerate(commits, 1):
            # Parse date from the commit info (first 10 characters of date)
            parts = commit.split(' ', 3)
            if len(parts) >= 3:
                git_date = parts[0]
                # Format it nicely like YYYY-MM-DD
                formatted_date = git_date.split('-')[0] + '-' + git_date.split('-')[1] + '-' + git_date.split('-')[2][:2]
                msg = ' '.join(parts[3:]) if len(parts) > 3 else ' '.join(parts[2:])
                changelog_entries.append(f"# {i}. [{formatted_date}] {msg}")
        
        # Handle empty entries case gracefully
        while len(changelog_entries) < 10:
            if len(changelog_entries) == 0:
                # Use a default for completely new files
                date_str = datetime.now().strftime('%Y-%m-%d')
                changelog_entries.append(f"# 1. [{date_str}] Initial implementation")
            else:
                # Pad remaining entries
                break
        
        changelog_header = '''"""
# CHANGELOG (last 10 broad changes):
'''

        for entry in changelog_entries:
            changelog_header += entry + '\n'
        changelog_header += '"""\n\n'
    
    # Check if file already has a docstring at the top
    if content.startswith('"""') or content.startswith("'''"):
        lines = content.split('\n')
        # Find the end of the initial docstring
        docstring_end = -1
        quote_type = None
        
        if lines[0].startswith('"""'):
            quote_type = '"""'
        elif lines[0].startswith("'''"):
            quote_type = "'''"
        
        if quote_type and quote_type in lines[0]:  # If it's a single-line docstring
            for i, line in enumerate(lines):
                if line.count(quote_type) >= 2:  # Has both opening and closing quotes
                    docstring_end = i
                    break
        else:  # Multi-line docstring
            for i, line in enumerate(lines):
                if quote_type in line:
                    docstring_end = i
                    if i > 0 or lines[i].count(quote_type) == 2:  # If found closing quote
                        break
            # If only the start of docstring is in first line, look for closing
            if docstring_end == -1 and quote_type:
                for i, line in enumerate(lines[1:], 1):
                    if quote_type in line:
                        docstring_end = i
                        break
        
        # Insert the changelog after the docstring
        if docstring_end >= 0:
            # Before docstring end + blank line + changelog + content after
            if lines[docstring_end] == '':
                # If there's already a blank line, just insert after
                new_content = '\n'.join(lines[:docstring_end+1]) + '\n' + changelog_header.rstrip('\n') + '\n' + '\n'.join(lines[docstring_end+1:])
            else:
                # Add blank line then changelog
                new_content = '\n'.join(lines[:docstring_end+1]) + '\n' + changelog_header.rstrip('\n') + '\n' + '\n'.join(lines[docstring_end+1:])
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
        else:
            # Should not happen if we detect initial docstring, but include for safety
            print("Warning: Could not locate end of initial docstring, prepending changelog")
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(changelog_header + content)
    else:
        # Insert changelog at the beginning of the file
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(changelog_header + content)
    
    print(f"Added changelog header to: {file_path}")


def process_directory(directory):
    """Process all Python files in the given directory"""
    for root, dirs, files in os.walk(directory):
        # Skip __pycache__ directories
        dirs[:] = [d for d in dirs if d != '__pycache__']
        
        for file in files:
            if file.endswith('.py') and file != '__init__.py':
                file_path = os.path.join(root, file)
                print(f"Processing: {file_path}")
                add_changelog_header(file_path)


if __name__ == "__main__":
    # Process the core directory
    core_dir = 'core'
    signals_dir = 'core/signals'
    
    print("Processing core/ directory...")
    process_directory(core_dir)
    
    print("Processing core/signals/ directory...")
    process_directory(signals_dir)
    
    print("Done!")