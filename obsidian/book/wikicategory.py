import requests
# todo
# https://en.wiktionary.org/w/index.php?title=Category:English_1-syllable_words&from=JW

p=True
def printonce(s):
    global p
    if p:
        print(s)
        p=False


def get_category_members(category, limit=500):
    base_url = "https://en.wiktionary.org/w/api.php"
    session = requests.Session()

    members = []
    extras = []
    cmcontinue = ''

    while True:
        params = {
            "action": "query",
            "format": "json",
            "list": "categorymembers",
            "cmtitle": category,
            "cmlimit": limit,
            "cmcontinue": cmcontinue
        }

        response = session.get(url=base_url, params=params)
        data = response.json()

        for page in data['query']['categorymembers']:
            title = page['title']
            if "Category:" in title or "Appendix:" in title :
                extras.append(page)
            else:
                members.append(title)

        if 'continue' not in data:
            break

        cmcontinue = data['continue']['cmcontinue']

    return members, extras

def write_to_markdown(members, file_name):
    members.sort()
    organized_members = {}

    # Organize members into subsections based on starting letter
    for member in members:
        letter = member[0].upper()
        if letter not in organized_members:
            organized_members[letter] = []
        organized_members[letter].append(member)

    # Write to Markdown file
    with open(file_name, 'w', encoding='utf-8') as file:
        for letter, titles in organized_members.items():
            file.write(f"## {letter}\n")
            for title in titles:
                file.write(f"- {title}\n")
            file.write("\n")

if __name__=="__main__":
  category = "Category:English_idioms"
  members, extras = get_category_members(category)
  print(extras)
  # Recurse into appendices
  markdown_file = 'Books/Idioms.md'
  write_to_markdown(members, markdown_file)


def get_urls_from_page_ids(page_ids):
    base_url = "https://en.wiktionary.org/w/api.php"
    session = requests.Session()

    urls = []

    for page_id in page_ids:
        params = {
            "action": "query",
            "prop": "info",
            "pageids": page_id,
            "inprop": "url",
            "format": "json"
        }

        response = session.get(url=base_url, params=params)
        data = response.json()

        for page in data['query']['pages'].values():
            urls.append(page['fullurl'])

    return urls