
from pycspwrapper import LVStat # pip install pycspwrapper
from mcp.server.fastmcp import FastMCP # pip install "mcp[cli]"
import requests
from typing import Any, Dict, List, Tuple

# Create an MCP server
mcp = FastMCP("CSPdemo", json_response=True)

@mcp.tool()
def get_topics(name:str = '') -> dict:
    """Available topics from Official Statistics Portal of Latvia (CSP or Centrālā statistikas pārvalde).
    
Args:
    name (str): name of the topic. If not defined, function will return all available topics.
Returns:
    dict: The dictionary of topics, where key is topic name and value is topic code.
Examples:
    >>> get_topics('vide')
    {'Vide': 'ENV'}
    >>> print(get_topics())
    {'Iedzīvotāji': 'POP', 'Darbs': 'EMP', 'Sociālā aizsardzība un veselība': 'VES',...
"""
    name_capit = name.capitalize()
    base_url = 'https://data.stat.gov.lv/api/v1/lv/OSP_PUB/'
    content = requests.get(base_url)
    content_short = {i['text']: i['id'] for i in content.json()}

    if name_capit in content_short.keys():
        return {name_capit: content_short[name_capit]}
    else:
        return content_short


@mcp.tool()
def get_topic_content(topic: str) -> dict:
    """Available contents of the topic from Official Statistics Portal of Latvia (CSP or Centrālā statistikas pārvalde).
    
Args:
    topic (str): topic code. Use get_topics to get topic code.
Returns:
    dict: The dictionary of the contents of the topic, where key is the topic content and value is the topic content code.
Examples:
    >>> # First get topic code
    ... get_topics('vide')
    {'Vide': 'ENV'}
    >>> # Then use this code to get content
    ... print(get_topic_content('ENV'))
    {'Vides konti': 'VI', 'Atkritumu apsaimniekošana': 'AK', 'Agro-vides rādītāji': 'AV',...
    >>> get_topics('Iedzīvotāji')
    {'Iedzīvotāji': 'POP'}
    >>> print(get_topic_content('POP'))
    {'Iedzīvotāju skaits un raksturojošie rādītāji': 'IR', 'Dzimstība': 'ID', 'Mirstība': 'IM', 'Nāves cēloņi': 'NC',...
"""
    base_url = 'https://data.stat.gov.lv/api/v1/lv/OSP_PUB/START/'
    content = requests.get(base_url+topic.upper())
    content_short = {i['text']: i['id'] for i in content.json()}
    return content_short

@mcp.tool()
def get_titles(topic_content_code:str = '',
               url:str = 'https://data.stat.gov.lv/api/v1/lv/OSP_PUB?query=*&filter=*') -> dict:
    """Available data (titles) from Official Statistics Portal of Latvia (CSP or Centrālā statistikas pārvalde).
    
    Args:
        topic_content_code (str): topic content code. Use get_topic_content to get topic content code.
        If not defined, function will return all available titles.
        url (str): URL from where to get list of available titles. Default value: 'https://data.stat.gov.lv/api/v1/lv/OSP_PUB?query=*&filter=*'.
    Returns:
        dict: The dictionary of the titles available from Official Statistics Portal,
        where key is the title name and value is the list of 4 elements: topic code, topic content code, topic sub-content code and report ID.
    Examples:
        >>> # First get topic code
        ... get_topics('Darbs')
        {'Darbs': 'EMP'}
        >>> # Then use this code to get content
        ... print(get_topic_content('EMP'))
        {'Darba samaksa (algas)': 'DS', 'Darbaspēka izmaksas': 'DI', 'Darbvietas un darba laiks': 'DV',...
        >>> # Then use this content code to extract report titles
        ... print(get_titles('DS'))
        {'Dzīvi un nedzīvi dzimušo skaits pēc dzimuma 1920 - 2020': ['POP', 'ID', 'IDS', 'IDS010'],...
    """
    alldb = requests.get(url)
    dict_result = {}
    for i in alldb.json():
        if topic_content_code == i['path'].split('/')[-2]:
            dict_result[i['title']] = [j for j in i['path'].split('/') if j]+[i['id']]
    return dict_result

@mcp.tool()
def get_query_values(topic_params: list[str] = []) -> List[Dict]:
    """Get query code and values for particular report.
    
    Args:
        topic_params (list[str]): arguments as a list that are needed for data extraction.
            Arguments in the list should be in the following order:
            - topic code,
            - topic content code,
            - topic sub-content code
            - report ID.
            These codes you can get from the function get_titles.
    Returns:
        Dict: A dictionary where each key is a query parameter code and the value is another dictionary mapping possible values to their descriptive texts.
    Examples:
        >>> # First get report topic parameters from get_titles
        ... print(get_titles('DS'))
        {'Dzīvi un nedzīvi dzimušo skaits pēc dzimuma 1920 - 2020': ['POP', 'ID', 'IDS', 'IDS010'],...
        >>> # Then use these values to get possible query values
        ... print(get_query_values(['POP', 'ID', 'IDS', 'IDS010']))
        {'SEX_NEWBORN': {'T': 'Pavisam', 'M': 'Vīrieši', 'F': 'Sievietes'}, 'ContentsCode': {'IDS010': 'Dzīvi dzimuši', 'IDS0101': 'Nedzīvi dzimuši', 'IDS0102': 'Nedzīvi dzimuši uz 1000 dzīvi dzimušiem'}, 
        'TIME': {'1920': '1920' ... '2024': '2024'}}
    """
    base_url = 'https://data.stat.gov.lv/api/v1/lv/OSP_PUB/START/'
    url = base_url + '/'.join(topic_params)
    response = requests.get(url)
    try:
        response.raise_for_status()  # Raises HTTPError for bad responses (4xx, 5xx)
        data = response.json()
        if 'variables' not in data:
            raise ValueError("Unexpected JSON structure: 'variables' key missing")
        
        result = {}

        for var in data['variables']:
            code = var.get('code', '')
            values = var.get('values', [])
            value_texts = var.get('valueTexts', [])
            # build dict mapping value -> valueText
            mapping = dict(zip(values, value_texts))
            result[code] = mapping
        
        return result
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Request failed: {e}")
    except ValueError as ve:
        raise RuntimeError(f"Parsing failed: {ve}")


def construct_csp_link(params: list[str]) -> str:
    base_url = 'https://data.stat.gov.lv/pxweb/lv/OSP_PUB/START__'
    mid_path = '__'.join(params[:3])
    last_part = params[3]
    return f"{base_url}{mid_path}/{last_part}/"

@mcp.tool()
def get_csp_data(lang: str = 'en', topic_params: list[str] = [], **kwargs) -> List[Dict]:
    """Get statistics from Official Statistics Portal of Latvia (CSP or Centrālā statistikas pārvalde). Use 'Source URL' from the Returns to cite the data source.
    
    Args:
        lang (str): Language. Default value 'en'.
        topic_params (list[str]): arguments as a list that are needed for data extraction.
            Arguments in the list should be in the following order:
            - topic code,
            - topic content code,
            - topic sub-content code
            - report ID.
            These codes you can get from the function get_titles.
        kwargs: Keyword arguments for query configuration.
            Possible query argument names and their possible values
            can be obtained using the function get_query_values.
    Returns:
        list: The list of the dictionaries, where dictionary's key 'key' contains query parameters and key 'values' contains values. First list element is data source (URL) from CSB.
    Examples:
        >>> topics = ['POP', 'IR', 'IRE', 'IRE010']
        >>> query_args = get_query_values(topics)
        >>> print(query_args)
        {'ETHNICITY': {'TOTAL': 'Pavisam', 'E_LAT': 'Latvieši', 'E_ABZ': 'Abāzi', 'E_ABK': 'Abhāzi',...
        'E_SWE': 'Zviedri', 'OTH': 'Cita tautība', 'UNK_NSP': 'Nezināma, neizvēlēta'}, 'ContentsCode': {'IRE010': 'Skaits'}, 
        'TIME': {'1935': '1935', ... '2025': '2025'}}
        >>> # Then use these codes and values to get data for example Latvians for years 2024 and 2025.
        >>> # Value text 'Latvieši' explains what 'E_LAT' means.
        >>> data = get_csp_data(
        ...     lang='en',
        ...     topic_params=topics,
        ...     ETHNICITY=['E_LAT'],
        ...     TIME=['2024', '2025']
        ... )
        >>> print(data[0])
        {'key': ['E_LAT', '2024'], 'values': ['1186337']}
    """
    csp2 = LVStat(lang, *topic_params)
    csp2.set_query(**kwargs)

    link = construct_csp_link(topic_params)
    
    cspdata2 = csp2.get_data()
    return [{"Source URL": link}] + cspdata2['data']

# Run with streamable HTTP transport
if __name__ == "__main__":
    mcp.run(transport="streamable-http")