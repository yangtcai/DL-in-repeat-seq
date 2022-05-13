'''
Author: yangtcai yangtcai@gmail.com
Date: 2022-05-12 22:31:30
LastEditors: yangtcai yangtcai@gmail.com
LastEditTime: 2022-05-13 20:53:11
FilePath: /undefined/Users/caiyz/Desktop/anno
Description: 这是默认设置,请设置`customMade`, 打开koroFileHeader查看配置 进行设置: https://github.com/OBKoro1/koro1FileHeader/wiki/%E9%85%8D%E7%BD%AE
'''

import requests, json
import csv 
from tqdm import tqdm

def get_subtype(accession_id: str):
    url = "https://dfam.org/api/families/" + accession_id
    response = requests.get(url)
    return response.json()['classification']

def get_annotation( sp, chr, op, ed):
    url = "https://dfam.org/api/annotations"
    params = {
        "assembly": sp,
        "chrom": chr,
        "start": op,
        "end": ed,
    }

    response = requests.get(url, params=params)
    results = response.json()
    annotations = []
    for hit in results['hits']:
        if hit['type'] == 'LTR':
            annotations.append([hit['ali_start'], hit['ali_end'], get_subtype(hit['accession'])])

    save_to_csv(annotations)

def save_to_csv(annotations):
    with open("test.csv","a+") as csvfile: 
        writer = csv.writer(csvfile)
        writer.writerows(annotations)

#get_annotation('hg38', 'chr3', 147733000, 147766820)

def get_all():
    for i in tqdm(range(0, 198295559, 100000)):
        get_annotation('hg38', 'chr3', i, i+100000)



get_all()