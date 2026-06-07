import json
import random
import time
from openai import OpenAI
from tqdm import tqdm
import re
import os
from datetime import datetime


def extract_data(response):
    response = response.split('</think>>')
    index = len(response) - 1
    pattern = r'条目\d+：(\[.*?\])'
    res = []
    entries = re.findall(pattern, response[index], re.DOTALL)
    for item in entries:
        item = item[1:len(item) - 1]
        res.append(item)
    return res


def get_data(tables, text, templates):
    client = OpenAI(
        base_url="http://localhost:11434/v1",
        api_key="sk-523765c5ade145fea7d4e8cb4ef95ecb"
    )
    response = client.chat.completions.create(
        model="gpt-oss:20b-40960",
        messages=[
            {"role": "user",
             "content": f"""
你是一个材料科学信息提取专家。接下来我会发给你一篇文字形式的论文，以及这篇论文以json结构提取的所有数据相关的表格，请根据以下规则，将论文原文中的信息转化或提取为标准化的逗号分隔数据行。
目标数据提取属性要求（必须严格遵守）：
Material,Tensile_name,Tensile_value,Tensile_unit,Yield_name,Yield_value,Yield_unit,Elongation_name,Elongation_value,Elongation_unit,H,B,C,N,O,F,Na,Mg,Al,Si,P,S,Cl,Ca,Ti,V,Cr,Mn,Fe,Co,Ni,Cu,Zn,As,Y,Zr,Nb,Mo,Sn,Sb,La,Ce,Ta,W,Pb,Bi,ProcessDescription
他们依次是：材料名称（论文中出现了并对其进行了实验的材料名称、由某种材料经过加工得到的中间标记样本材料也作为单独的一条信息提取单独的条目，一定要尽可能详细的区分），张力名称，张力值，张力单位，屈服名称，屈服值，屈服单位，伸长率名称，伸长率值，伸长率单位，H元素含量，B元素含量，C元素含量，N元素含量，O元素含量，F元素含量，Na元素含量，Mg元素含量，Al元素含量，Si元素含量，P元素含量，S元素含量，Cl元素含量，Ca元素含量，Ti元素含量，V元素含量，Cr元素含量，Mn元素含量，Fe元素含量，Co元素含量，Ni元素含量，Cu元素含量，Zn元素含量，As元素含量，Y元素含量，Zr元素含量，Nb元素含量，Mo元素含量，Sn元素含量，Sb元素含量，La元素含量，Ce元素含量，Ta元素含量，W元素含量，Pb元素含量，Bi元素含量，该材料相关的所有加工工艺以及完整流程\n"
注意下述重要的要求：
1、你**不会直接得到这些字段**，而是需要从论文原文中自动识别并提取。
2、这些属性中最重要的是‘ProcessDescription’字段，这个属性需要包括论文中关于该名称材料参与了的加工工艺的完整描述整理，该属性字段的整体逻辑应该是对某种材料进行了何种操作，必须有前置和后续的结果，得到了什么结果或者材料，整体有上句的若干步骤组成，必须包括所有这种材料参与了的加工工艺；如果他是某种中间标记样本材料，那么你要将得到他的工艺也加入到该样本的条目的工艺流程属性中，如果他后续还参与了另外的加工工艺，那么后续的加工工艺也要加入到该名称材料的条目的工艺流程属性中。一定要完整且尽可能的多，整理为没有任何工艺遗漏的文本。
3、让我单独给你一个‘ProcessDescription’属性的例子：{templates[0]}。
4、这是另一个ProcessDescription’属性的例子：{templates[1]}。
5、按照我给你的工艺流程例子的描述逻辑来提取ProcessDescription属性，整体逻辑为三步关键词：上一步骤材料+工艺->当前条目材料。我会给你论文中使用到的表格，如果论文中描工艺流程中涉及到了表格，那么你需要将表格中的具体内容也加入到该属性的描述中以进行详细的区分，而不要出现‘如图...、如表...’等类似字样，而是将表中信息提取后整合到文本中，这是一个以json数组形式提取的论文中的所有表格的例子，你可以用于参考表格的形式和属性：{templates[2]}。
6、工艺的提取文本中不能有描指代不清或者容易混淆的描述，提取的内容需要脱离论文后也能被阅读者清晰的理解。例如:'将样品在1200°C退火1小时，随后进行三次热轧',该描述中的‘样品’在脱离论文后则指代不清，你需要在提取时根据论文清晰指代该样品是什么。
7、工艺的提取文本中需要保证流程的完整性。例如'在HR多层钢基础上，于485°C加热4小时后空气冷却得到HR-A多层钢'，如果‘HR多层钢’不是某种未经过任何加工的原材料，那么在脱离论文后读者无法理解‘HR多层钢’是通过何种工艺得来的。那你就需要将通过何种材料经过何种工艺或操作得到‘HR多层钢’该中间材料详细的表述后也整理为工艺描述的文本并加入到后续工艺描述之前。
8、论文中所有出现的材料你都需要提取，你需要确保在工艺流程中提到的对某某某材料进行何种工艺中的某某某材料是一条单独的条目。
9、严格按照下列格式输出：‘条目1：[材料名称：xx，张力名称：xx，......] \n条目2：[材料名称：xx，张力名称：xx，......] \n’，要求属性顺序与例子对应，不要有任何多余的字符和描述。
10、严格使用中文。
因此，你的任务是：
- 阅读我提供的论文原文段落，
- 从中识别多组：材料名称、TS（抗拉强度）、YS（屈服强度）、f-EL（断裂延伸率），
- 提取该材料的化学组成元素信息，如果某些元素未找到置为0即可，
- 重构或提取完整的制备工艺描述文本,工艺流程指的是对某种材料进行了何种操作，得到了什么结果，这个属性一定要尽可能的完整且多，只要你发现了某个名称材料，那么就把论文中所有有关该材料出现或参与的工艺整理为至该属性字段，
- 严格按照下列格式输出：条目1：[材料名称：xx，张力名称：xx，......] \n条目2：[材料名称：xx，张力名称：xx，......] \n，要求属性顺序与例子对应，不要有任何多余的字符和描述。
- 以中文生成。
Few-Shot 示例：
提取示例：
这是一个例子，对应了上面的各个属性的提取结果：C300,TS (MPa),1055,MPa,YS (MPa),843,MPa,f-EL (%),20,%,0,0,0.03,0,0,0,0,0,0.1,0.1,0,0,0,0,0.7,0,0,0.1,67.2,9,18,0,0,0,0,0,0,5,0,0,0,0,0,0,0,0,{templates[0]}"
现在请处理以下论文原文并严格按照给定的格式生成：
文本：{text}
这是这篇论文以json形式附带的表格：
表格：{tables}
"""},

        ],
        temperature=0,  # 控制生成多样性
        max_tokens=8192  # 最大生成 token 数
    )
    print(response.choices[0].message.content)
    return response.choices[0].message.content


# 示例用法

def main(output_folder, path, file):
    path = f'{path}/{file}'
    with open("E:\Code\datapull\main\work/20260107/xml2jsonRes/10.1016_j.addma.2018.04.031.json", 'r',
              encoding='utf-8') as f:
        tabel0 = json.load(f)['tables']

    templates = [
        "原始G91钢板（500×250×30 mm）未经任何热处理，直接用于实验。",
        "将G91钢板切割成50×20×22 mm的小块，先在1150°C加热1h形成奥氏体，随后在1050°C进行中间轧制，最后在650°C加热1h并空气冷却，得到R1050-T650样品。",
        tabel0
    ]

    # 加载第二个 JSON（用于 tables 和 text）
    with open(path, 'r',
              encoding='utf-8') as f:
        data = json.load(f)
    tables = data['tables']
    text = data['predata']
    response = get_data(tables, text, templates)
    # 保存结果
    with open(f"{output_folder}/{file}", 'w', encoding='utf-8') as f:
        if len(response) > 20:
            json.dump(extract_data(response), f, ensure_ascii=False, indent=4)
        else:
            json.dump(response, f, ensure_ascii=False, indent=4)


if __name__ == "__main__":
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_folder = f'E:\Code\datapull\main\work/20260108/extract_data_{timestamp}'
    os.makedirs(output_folder, exist_ok=True)
    input_folder = "E:\Code\datapull\main\work/20260108/xml2jsonRes"
    files = os.listdir(input_folder)
    set1 = set(files)
    with open("E:\Code\datapull\main\knowledge_extraction\extractedDoiSet.json", "r", encoding="utf-8") as f:
        set2 = json.load(f)
        set22 = []
        for doi in set2:
            set22.append(doi+".json")
        set2 = set(set22)
    # set1 中有、但 set2 中没有的文件名
    files = sorted(set1 - set2)
    progressbar = tqdm(total=len(files), desc="执行进度", colour='white')
    for file1 in files:
        with open("E:\Code\datapull\main\knowledge_extraction\extractedDoiSet.json", "r", encoding="utf-8") as f:
            extracted_files = set(json.load(f))
        if file1 not in extracted_files:
            main(output_folder, "E:\Code\datapull\main\work/20260108/xml2jsonRes", file1)
            extracted_files.add(file1.split(".json")[0])
        else:
            print(f"doi:{file1}：当前文档已经存在提取结果")
        with open("E:\Code\datapull\main\knowledge_extraction\extractedDoiSet.json", "w", encoding="utf-8") as f:
            json.dump(list(extracted_files), f, ensure_ascii=False, indent=2)
        progressbar.update(1)
    progressbar.close()
    print("Done")

    # main('./', "/home/chengyiao/桌面/Code/datapull/v_xml/xml2jsonRes", '10.1016_j.addma.2021.102068.json')