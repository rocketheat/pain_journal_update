import feedparser
import re
import requests
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET
import time
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from openai import OpenAI
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# --- Configuration ---
FEEDS = {
    'Interventional Pain Medicine': 'https://rss.sciencedirect.com/publication/science/27725944',
    'Regional Anesthesia & Pain Medicine': 'https://rapm.bmj.com/rss/current.xml',
    'Pain Medicine': 'https://academic.oup.com/rss/site_5414/3275.xml',
    'Pain Practice': 'https://onlinelibrary.wiley.com/action/showFeed?jc=15332500&type=etoc&feed=rss',
    'Pain': 'https://journals.lww.com/pain/_layouts/15/OAKS.Journals/feed.aspx?FeedType=CurrentIssue',
    'Journal of Pain Research': 'https://www.tandfonline.com/feed/rss/djpr20'
}

# Keywords for filtering spine-related articles from general neurosurgery publications
SPINE_KEYWORDS = [
    'spine', 'spinal', 'cervical', 'thoracic', 'lumbar', 'sacral', 'vertebr',
    'disc', 'disk', 'scoliosis', 'kyphosis', 'lordosis', 'myelopathy',
    'radiculopathy', 'lumbar stenosis', 'cervical stenosis', 'fusion', 'laminectomy', 'discectomy',
    'foraminotomy', 'decompression', 'interbody', 'pedicle', 'screw',
    'cage', 'rod', 'plate', 'cauda equina', 'thecal sac'
]

# Expanded exclusion keywords
EXCLUSION_KEYWORDS = [
    'cerebrospinal',
    'deep brain stimulation', 'dbs', 'brain stimulation',
    'subthalamic', 'subthalamic nucleus',
    'cerebral', 'cerebrum', 'cerebellum',
    'transcranial', 'intracranial',
    'pneumocephalus', 'cranial', 'craniotomy',
    'electroencephalogram', 'eeg',
    'brain', 'brain surgery',
    'neurosurgery AND NOT spine', 'neurosurgical AND NOT spine',
    'parkinson', 'alzheimer', 'epilepsy', 'seizure',
    'glioma', 'meningioma', 'brain tumor'
]

# Publication types with corresponding color codes
PUBLICATION_TYPES = {
    'Case Report': '#98fb98',  # Pale Green
    'Case Series': '#8fbc8f',  # Dark Sea Green
    'Retrospective Case Control': '#b0c4de',  # Light Steel Blue
    'Retrospective Cohort': '#87cefa',  # Light Sky Blue
    'Cross-sectional Study': '#add8e6',  # Light Blue
    'Prospective Cohort': '#e0b0ff',  # Mauve
    'Prospective Study': '#dda0dd',  # Plum
    'Randomized Clinical Trial': '#ffa07a',  # Light Salmon
    'Non-randomized Trial': '#f4a460',  # Sandy Brown
    'Systematic Review': '#ffb6c1',  # Light Pink
    'Meta-Analysis': '#ffd700',  # Gold
    'Narrative Review': '#eee8aa',  # Pale Goldenrod
    'Clinical Practice Guideline': '#98fb98',  # Pale Green
    'Technical Note': '#d3d3d3',  # Light Gray
    'Biomechanical Study': '#afeeee',  # Pale Turquoise
    'Cadaveric Study': '#bc8f8f',  # Rosy Brown
    'Animal Study': '#f0e68c',  # Khaki
    'Basic Science Research': '#7fffd4',  # Aquamarine
    'Quality Improvement': '#ff7f50',  # Coral
    'Cost-effectiveness Analysis': '#da70d6',  # Orchid
    'Other': '#f5f5f5'  # White Smoke (default)
}

API_KEY = os.getenv("NCBI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")


def fetch_emails_from_airtable():
    AIRTABLE_API_KEY = "patSomBtahHbQFGKa.b100ff193b7eaca1874be3170702eeaa4a480529ddcb8c012b6d408ae2d1ae5a"
    AIRTABLE_BASE_ID = "app3JX8ma5JUX9TmJ"
    AIRTABLE_TABLE_NAME = "spine_registry_data"

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
    }

    response = requests.get(url, headers=headers)
    records = response.json().get("records", [])

    emails = []
    for record in records:
        email = record['fields'].get("Email")
        if email:
            emails.append(email.strip())
    return emails


EMAIL_RECEIVER = fetch_emails_from_airtable()

print(EMAIL_RECEIVER)


# --- Helper Functions ---
def is_spine_related(title, description=None):
    """
    Check if an article is spine-related based on keywords in title and description,
    while excluding brain-specific articles that aren't related to the spine.
    """
    if not title:
        return False

    # Combine title and description (if available) for search
    search_text = title.lower()
    if description:
        search_text += " " + description.lower()

    # Check for spine-related keywords first
    has_spine_keyword = False
    for keyword in SPINE_KEYWORDS:
        if keyword.lower() in search_text:
            has_spine_keyword = True
            break

    # If no spine keywords found, return False immediately
    if not has_spine_keyword:
        return False

    # Now check for exclusion keywords
    for ex_keyword in EXCLUSION_KEYWORDS:
        if ex_keyword.lower() in search_text:
            return False  # Exclude this article

    # Article has spine keywords and no exclusion keywords
    return True


def get_pmid(title, journal=None):
    search_term = quote_plus(title)
    if journal:
        search_term += f"+AND+{quote_plus(journal)}[journal]"
    url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={search_term}&retmode=xml"
    if API_KEY:
        url += f"&api_key={API_KEY}"
    try:
        response = requests.get(url)
        if response.status_code == 200:
            root = ET.fromstring(response.content)
            id_list = root.find('.//IdList')
            if id_list is not None and len(id_list):
                return id_list[0].text
    except:
        pass
    return None


def get_abstract_and_authors(pmid):
    if not pmid:
        return None, None, None

    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    params = {"db": "pubmed", "id": pmid, "retmode": "xml"}
    if API_KEY:
        params["api_key"] = API_KEY
    time.sleep(0.34)

    try:
        response = requests.get(url, params=params)
        if response.status_code != 200:
            return None, None, None

        root = ET.fromstring(response.content)

        # Get abstract
        abstract_sections = root.findall(".//AbstractText")
        if not abstract_sections or all(not section.text for section in abstract_sections):
            return None, None, None  # No abstract available

        abstract = " ".join([section.text for section in abstract_sections if section.text])

        # Get authors
        author_elements = root.findall(".//Author")
        authors = []

        for author in author_elements:
            last_name = author.find(".//LastName")
            first_name = author.find(".//ForeName")
            initials = author.find(".//Initials")

            if last_name is not None and last_name.text:
                author_name = last_name.text
                if initials is not None and initials.text:
                    author_name += f" {initials.text}"
                authors.append(author_name)

        if not authors:
            return abstract, None, None

        first_author = authors[0] if authors else None
        last_author = authors[-1] if len(authors) > 1 else None

        return abstract, first_author, last_author

    except Exception as e:
        print(f"Error fetching abstract and authors: {str(e)}")
        return None, None, None


def determine_publication_type(abstract):
    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": f"""You are an expert in medical research classification.

Based on the following abstract, classify the publication type into ONE of these categories:
- Case Report
- Case Series
- Retrospective Case Control
- Retrospective Cohort
- Cross-sectional Study
- Prospective Cohort
- Prospective Study
- Randomized Clinical Trial
- Non-randomized Trial
- Systematic Review
- Meta-Analysis
- Narrative Review
- Clinical Practice Guideline
- Technical Note
- Biomechanical Study
- Cadaveric Study
- Animal Study
- Basic Science Research
- Quality Improvement
- Cost-effectiveness Analysis
- Other

Return ONLY the classification as a single term with no explanation or additional text.

Abstract:
{abstract}
"""
            }]
        )
        pub_type = response.choices[0].message.content.strip()

        # Normalize the publication type to match our defined types
        for defined_type in PUBLICATION_TYPES.keys():
            if defined_type.lower() in pub_type.lower():
                return defined_type

        return "Other"
    except Exception as e:
        print(f"Error determining publication type: {str(e)}")
        return "Other"


def summarize_and_context(abstract, first_author, last_author):
    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": f"""You are an expert scientific assistant.

Given this abstract, generate:
1. A section that summarizes the core findings in formal academic language
2. A section that explains why the study is important and how it relates to previous spine surgery or spine research literature

Format your response so that:
- The first section begins with "Summary" (no colon)
- The second section begins with "Context" (no colon)
- Both headings should be in a formal style that a medical journal would use
- Use a concise, authoritative tone throughout

Do NOT use any asterisks or special formatting characters in your response.

Abstract:
{abstract}
"""
            }]
        )

        # Get the response content
        content = response.choices[0].message.content.strip()

        # Replace "Summary" and "Context" with styled versions
        content = content.replace("Summary",
                                  '<div style="font-weight: bold; color: #333; margin-top: 12px; margin-bottom: 6px;">Summary<div style="width: 60px; height: 2px; background-color: #2e8b57; margin-top: 3px;"></div></div>')
        content = content.replace("Context",
                                  '<div style="font-weight: bold; color: #333; margin-top: 12px; margin-bottom: 6px;">Context<div style="width: 60px; height: 2px; background-color: #2e8b57; margin-top: 3px;"></div></div>')

        return content
    except Exception as e:
        return f"Error generating summary and context: {str(e)}"


from datetime import datetime


def generate_html(articles, summaries_and_contexts, publication_types):
    now = datetime.now().strftime("%B %d, %Y")

    # Group articles by journal
    journal_groups = {}
    for idx, article in enumerate(articles):
        journal = article['journal']
        if journal not in journal_groups:
            journal_groups[journal] = []

        # Add the index to keep track of the original order
        journal_groups[journal].append((idx, article))

    # Sort journals alphabetically
    sorted_journals = sorted(journal_groups.keys())

    # Create a mapping from original index to new index for anchor links
    article_index_map = {}
    current_index = 0
    for journal in sorted_journals:
        for idx_article_tuple in journal_groups[journal]:
            orig_idx = idx_article_tuple[0]
            article_index_map[orig_idx] = current_index
            current_index += 1

    html = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Cleveland Clinic Alumni Journal Update</title>
        <!-- Fix for email client anchor links -->
        <style>
            a[name], a[id] {{ 
                color: inherit;
                text-decoration: none; 
                display: block;
                position: relative;
                top: -20px;
                visibility: hidden;
            }}
            .return-link {{
                color: #0070C0 !important;
                text-decoration: none !important;
                font-size: 12px !important;
            }}
        </style>
    </head>
    <body style="font-family: Arial, sans-serif; background-color: #f9f9f9; padding: 20px; margin: 0;">
        <table cellspacing="0" cellpadding="0" border="0" width="100%" style="max-width: 600px; margin: 0 auto;">
            <tr>
                <td>
                    <a name="top" id="top"></a>
                    <h1 style="color: #0070C0;">🧠 Cleveland Clinic Alumni Journal Update</h1>
                    <p><i>Generated on {now}</i></p>
                </td>
            </tr>

            <!-- Table of Contents grouped by journal -->
            <tr>
                <td style="padding-bottom: 20px;">
                    <table cellspacing="0" cellpadding="0" border="0" width="100%" style="background: #fff; border: 1px solid #ddd; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1);">
                        <tr>
                            <td style="padding: 15px;">
                                <h2 style="margin-top: 0; font-size: 18px; color: #333;">In This Update:</h2>
    """

    # Generate Table of Contents grouped by journal
    for journal in sorted_journals:
        if not journal_groups[journal]:  # Skip empty journals
            continue

        html += f"""
                                <h3 style="margin-top: 15px; margin-bottom: 10px; font-size: 16px; color: #2e8b57; border-bottom: 1px solid #2e8b57; padding-bottom: 5px;">
                                    {journal}
                                </h3>
                                <table cellspacing="0" cellpadding="0" border="0" width="100%">
        """

        for orig_idx, article in journal_groups[journal]:
            title = article['title']
            new_idx = article_index_map[orig_idx]
            toc_id = f"toc_article{new_idx + 1}"
            anchor_name = f"article{new_idx + 1}"
            pub_type = publication_types[orig_idx]
            pub_type_color = PUBLICATION_TYPES.get(pub_type, PUBLICATION_TYPES['Other'])

            html += f"""
                                    <tr>
                                        <td style="padding: 6px 0;">
                                            <table cellspacing="0" cellpadding="0" border="0" width="100%">
                                                <tr>
                                                    <td width="30" style="vertical-align: top;">
                                                        <span style="display: inline-block; width: 24px; height: 24px; line-height: 24px; text-align: center; background-color: {pub_type_color}; color: white; border-radius: 50%; margin-right: 8px; font-weight: bold; font-size: 14px;">{new_idx + 1}</span>
                                                    </td>
                                                    <td>
                                                        <a name="{toc_id}" id="{toc_id}"></a>
                                                        <a href="#{anchor_name}" onClick="document.location.hash='{anchor_name}'; return false;" style="color: #0070C0; text-decoration: none; font-weight: bold;">
                                                            {title}
                                                        </a>
                                                    </td>
                                                </tr>
                                            </table>
                                        </td>
                                    </tr>
            """

        html += """
                                </table>
        """

    html += """
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
    """

    # Generate all articles in order by journal
    current_index = 0
    for journal in sorted_journals:
        if not journal_groups[journal]:  # Skip empty journals
            continue

        # Add journal header with anchor
        html += f"""
            <tr>
                <td style="padding-bottom: 15px;">
                    <a name="journal_{journal.replace(' ', '_')}" id="journal_{journal.replace(' ', '_')}"></a>
                    <h2 style="margin: 0; color: #2e8b57; font-size: 20px; border-bottom: 2px solid #2e8b57; padding-bottom: 8px;">
                        {journal}
                    </h2>
                </td>
            </tr>
        """

        # Add articles for this journal
        for orig_idx, article in journal_groups[journal]:
            full_text = summaries_and_contexts[orig_idx]

            # Replace ** with proper HTML formatting
            full_text_html = full_text.replace('\n', '<br/>')

            # Display authors from article data
            first_author = article.get('first_author', '')
            last_author = article.get('last_author', '')

            # Add author information
            author_info = ""
            if first_author and last_author and first_author != last_author:
                author_info = f"<p><b>Authors:</b> {first_author} ... {last_author}</p>"
            elif first_author:
                author_info = f"<p><b>Author:</b> {first_author}</p>"

            # Calculate the new index for this article
            new_idx = article_index_map[orig_idx]
            anchor_name = f"article{new_idx + 1}"
            toc_id = f"toc_article{new_idx + 1}"

            # Get publication type and its color
            pub_type = publication_types[orig_idx]
            pub_type_color = PUBLICATION_TYPES.get(pub_type, PUBLICATION_TYPES['Other'])

            html += f"""
                <tr>
                    <td style="padding-bottom: 20px;">
                        <!-- Proper anchor placement for all email clients -->
                        <a name="{anchor_name}" id="{anchor_name}"></a>
                        <div id="{anchor_name}_target" style="position: relative; top: -30px;"></div>

                        <table cellspacing="0" cellpadding="0" border="0" width="100%" style="background: #fff; border: 1px solid {pub_type_color}; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1);">
                            <!-- Header with number and title -->
                            <tr>
                                <td style="padding: 12px 15px; background-color: #f0f8ff; border-top-left-radius: 8px; border-top-right-radius: 8px; border-bottom: 1px solid #e0e0e0;">
                                    <table cellspacing="0" cellpadding="0" border="0" width="100%">
                                        <tr>
                                            <td width="40" style="vertical-align: top;">
                                                <div style="width: 30px; height: 30px; background-color: {pub_type_color}; border-radius: 50%; color: white; font-weight: bold; font-size: 16px; text-align: center; line-height: 30px;">{new_idx + 1}</div>
                                            </td>
                                            <td style="vertical-align: middle;">
                                                <div style="font-weight: bold; font-size: 16px; line-height: 1.3;">{article['title']}</div>
                                            </td>
                                            <td width="80" style="vertical-align: middle; text-align: right;">
                                                <!-- Cross-email-client compatible return link -->
                                                <a href="#{toc_id}" class="return-link" style="color: #0070C0; text-decoration: none; font-size: 12px;" onClick="
                                                    try {{
                                                        document.location.hash='#{toc_id}';
                                                        return false;
                                                    }} catch(e) {{
                                                        return true;
                                                    }}">
                                                    ↑ Return
                                                </a>
                                            </td>
                                        </tr>
                                    </table>
                                </td>
                            </tr>

                            <!-- Article content -->
                            <tr>
                                <td style="padding: 15px;">
                                    <table cellspacing="0" cellpadding="0" border="0" width="100%">
                                        <tr>
                                            <td width="80" style="vertical-align: top; color: #666;"><b>Journal:</b></td>
                                            <td style="vertical-align: top;">{article['journal']}</td>
                                        </tr>
                                        <tr>
                                            <td width="80" style="vertical-align: top; padding-top: 5px; color: #666;"><b>Type:</b></td>
                                            <td style="vertical-align: top; padding-top: 5px;">
                                                <span style="display: inline-block; padding: 2px 8px; background-color: {pub_type_color}; color: white; border-radius: 4px; font-size: 12px;">{pub_type}</span>
                                            </td>
                                        </tr>
                                        <tr>
                                            <td width="80" style="vertical-align: top; padding-top: 5px; color: #666;"><b>PMID:</b></td>
                                            <td style="vertical-align: top; padding-top: 5px;">
                                                <a href="https://pubmed.ncbi.nlm.nih.gov/{article['pmid']}/" target="_blank" style="color: #0070C0; text-decoration: none;">{article['pmid']}</a>
                                            </td>
                                        </tr>
                                        <tr>
                                            <td colspan="2" style="padding-top: 5px;">
                                                {author_info}
                                            </td>
                                        </tr>
                                        <tr>
                                            <td colspan="2" style="padding-top: 10px;">
                                                <div style="line-height: 1.5; color: #333;">{full_text_html}</div>
                                            </td>
                                        </tr>
                                    </table>
                                </td>
                            </tr>
                        </table>
                    </td>
                </tr>
            """

            current_index += 1

    html += """
            </table>
        </body>
    </html>
    """
    return html


def send_email(subject, html_content):
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = EMAIL_USER
    message["To"] = ", ".join(EMAIL_RECEIVER)
    part = MIMEText(html_content, "html")
    message.attach(part)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_USER, EMAIL_RECEIVER, message.as_string())


# --- Main Logic ---
def main():
    articles = []
    summaries_and_contexts = []
    publication_types = []

    for journal, feed_url in FEEDS.items():
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:15]:  # Top 15 per journal
            title = getattr(entry, 'title', 'No Title')
            description = getattr(entry, 'description', '')

            # For Neurosurgery and World Neurosurgery journal, apply spine filter
            # if journal == 'Neurosurgery' and not is_spine_related(title, description):
            #     continue
            # if journal == 'World Neurosurgery' and not is_spine_related(title, description):
            #     continue

            pmid = get_pmid(title, journal)

            if pmid:
                abstract, first_author, last_author = get_abstract_and_authors(pmid)

                # Skip articles with no abstract
                if not abstract:
                    continue

                # Determine publication type
                pub_type = determine_publication_type(abstract)
                publication_types.append(pub_type)

                # Add article with author information
                articles.append({
                    "journal": journal,
                    "title": title,
                    "pmid": pmid,
                    "first_author": first_author,
                    "last_author": last_author
                })

                # Generate summary and context for the article
                full_summary_context = summarize_and_context(abstract, first_author, last_author)
                summaries_and_contexts.append(full_summary_context)

    if articles:  # Only send email if we have articles with abstracts
        html_email = generate_html(articles, summaries_and_contexts, publication_types)
        send_email("Monthly Spine Journal Update", html_email)
    else:
        print("No articles with abstracts found.")


if __name__ == "__main__":
    main()