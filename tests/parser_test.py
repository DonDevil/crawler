from parsers.html_link_extractor import HTMLLinkExtractor
import requests

url = "https://saec.ac.in"

html = requests.get(url).text

extractor = HTMLLinkExtractor()

links = extractor.extract_links(html, url)

for link in list(links)[:10]:
    print(link)