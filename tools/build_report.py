"""Generate editable native LaTeX from the approved Markdown and ZJU template."""
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'report'
TEMPLATE = ROOT / '浙江大学课程论文模板'


def escape(text):
    replacements = {'\\':r'\textbackslash{}','&':r'\&','%':r'\%','$':r'\$','#':r'\#','_':r'\_','{':r'\{','}':r'\}','~':r'\textasciitilde{}','^':r'\textasciicircum{}'}
    return ''.join(replacements.get(c,c) for c in text)


def inline(text):
    chunks=re.split(r'(\*\*.*?\*\*|`[^`]+`|\[[^\]]+\]\(https?://[^)]+\)|https?://[^\s*]+)',text)
    output=[]
    for chunk in chunks:
        if chunk.startswith('**'):output.append(r'\textbf{'+inline(chunk[2:-2])+'}')
        elif chunk.startswith('`'):
            code = chunk[1:-1]
            if '/' not in code and len(code) < 35:
                output.append(r'\texttt{'+escape(code).replace('-', r'-\allowbreak{}')+'}')
            else:
                output.append(r'\path{'+code+'}')
        elif re.fullmatch(r'\[[^\]]+\]\(https?://[^)]+\)',chunk):
            m=re.match(r'\[([^\]]+)\]\(([^)]+)\)',chunk)
            if m[1] in ('1','2','3'):output.append(r'\cite{'+{'1':'shield','2':'sentry','3':'cache'}[m[1]]+'}')
            else:output.append(r'\href{'+m[2]+'}{'+escape(m[1])+'}')
        elif chunk.startswith('http'):output.append(r'\url{'+chunk+'}')
        else:output.append(escape(chunk))
    return ''.join(output)


def table(lines,number):
    rows=[[c.strip() for c in line.strip().strip('|').split('|')] for line in lines]
    rows=[rows[0]]+rows[2:];n=len(rows[0])
    widths={2:[4,10.5],3:[6,4.4,4.4],4:[5.2,3.2,3.2,3.2],5:[0.8,4.3,3,3,3]}[n]
    spec=''.join(r'>{\raggedright\arraybackslash}p{'+str(w)+'cm}' for w in widths)
    captions={1:'三种程序在案例六中的处理过程',2:'八个案例的任务结果',3:'工具与模型调用及资源消耗',4:'提案类别与检查次数',5:'实现与证据索引'}
    header=' & '.join(r'\textbf{'+inline(c)+'}' for c in rows[0])+r' \\ \hline'
    output=[r'\begingroup',r'\footnotesize' if n==2 else r'\small',r'\setlength{\tabcolsep}{4pt}',r'\renewcommand{\arraystretch}{1.25}',r'\begin{longtable}{'+spec+'}',r'\caption{'+captions[number]+r'}\label{tab:'+str(number)+r'}\\',r'\hline',header,r'\endfirsthead',r'\multicolumn{'+str(n)+r'}{c}{表 \thetable\ （续）}\\\hline',header,r'\endhead',r'\hline\endfoot']
    output+=[' & '.join(inline(c) for c in row)+r' \\' for row in rows[1:]]
    output += [r'\end{longtable}',r'\endgroup']
    return '\n'.join(output)


def build():
    for name in ['zju_logo.png','zju_char.png']:shutil.copyfile(TEMPLATE/'figures'/name,REPORT/'figures'/name)
    (REPORT/'zjureport.sty').write_text((TEMPLATE/'zjureport.sty').read_text().replace(r'\RequirePackage{fontawesome}', '% Unused icon package omitted for font compatibility.').replace('bookmarksnumbered=blue', 'bookmarksnumbered=true'))
    preamble=r'''%!TeX program = xelatex
\documentclass[12pt,a4paper,UTF8,fontset=none]{ctexart}
\usepackage{zjureport}
\usepackage{longtable,array,float,needspace}
\setmainfont{Times New Roman}
\setCJKmainfont{Songti SC}
\setCJKmonofont{Songti SC}
\setCJKfamilyfont{zhsong}{Songti SC}
\setCJKfamilyfont{zhhei}[AutoFakeBold=2]{Songti SC}
\setCJKfamilyfont{zhkai}{Songti SC}
\setCJKfamilyfont{zhfs}{Songti SC}
\providecommand{\songti}{\CJKfamily{zhsong}}
\providecommand{\heiti}{\CJKfamily{zhhei}}
\providecommand{\kaishu}{\CJKfamily{zhkai}}
\providecommand{\fangsong}{\CJKfamily{zhfs}}
\setlength{\headheight}{16pt}
\setlength{\emergencystretch}{3em}
\Urlmuskip=0mu plus 2mu
\def\UrlBreaks{\do\/\do\-\do\_\do\.}
\hypersetup{colorlinks=true,linkcolor=black,citecolor=black,urlcolor=blue,pdfauthor={}}
\fancyhead[C]{\songti 工具失败恢复对照实验}
\fancyfoot[C]{\thepage}
\title{\vspace{0.8cm}\heiti\LARGE\textbf{〔课程名称待补〕课程报告}\par
\vspace{0.8cm}\songti\Large 工具调用失败后，有必要让 AI 决定下一步吗？\par
\vspace{0.3cm}\large ——一次固定规则与模型决策的对照实验\vspace{1.4cm}}
\author{\songti\large 姓名\ \dlmu[8cm]{〔待补〕}\\[0.35cm]
学号\ \dlmu[8cm]{〔待补〕}\\[0.35cm]
院所\ \dlmu[8cm]{〔待补〕}\\[0.35cm]
课程\ \dlmu[8cm]{〔待补〕}\\[0.35cm]
授课教师\ \dlmu[7.1cm]{〔待补〕}}
\date{2026年11月9日\\[0.6cm]\small 公开项目仓库\\\url{https://github.com/YoreJ/-}}
\begin{document}
\cover
\thispagestyle{empty}
\clearpage
\pagenumbering{arabic}
\tableofcontents
\clearpage
\nocite{shield,sentry,cache}
'''
    lines=(REPORT/'report.md').read_text().splitlines();i=1;output=[];number=0;references=False
    while i<len(lines):
        line=lines[i].strip()
        if not line:output.append('');i+=1;continue
        if line.startswith('```mermaid'):
            i+=1
            while lines[i].strip()!='```':i+=1
            output.append(r'\begin{figure}[H]\centering\includegraphics[width=0.94\textwidth]{figures/recovery-flow.png}\caption{历史检查器的工具动作处理流程}\label{fig:flow}\end{figure}')
        elif line.startswith('|'):
            group=[]
            while i<len(lines) and lines[i].strip().startswith('|'):group.append(lines[i]);i+=1
            number+=1;output.append(table(group,number));continue
        elif line.startswith('## 参考文献'):
            references=True;output.append(r'\clearpage\addcontentsline{toc}{section}{参考文献}\reference')
        elif references and re.match(r'\[[123]\]',line):pass
        elif line.startswith('## '):
            title=re.sub(r'^\d+[　 ]+','',line[3:])
            if title.startswith('附录'):output.append(r'\clearpage\section*{'+escape(title)+r'}\addcontentsline{toc}{section}{'+escape(title)+'}')
            else:output.append(r'\section{'+escape(title)+'}')
        elif line.startswith('### '):output.append(r'\Needspace{6\baselineskip}\subsection{'+escape(re.sub(r'^\d+\.\d+[　 ]+','',line[4:]))+'}')
        else:output.append(inline(line)+'\n')
        i+=1
    (REPORT/'正式报告.tex').write_text(preamble+'\n'.join(output)+'\n\\end{document}\n')
    (REPORT/'reference.bib').write_text(r'''@misc{shield,
 author={Mohammed Alshiekh and Roderick Bloem and Ruediger Ehlers and Bettina Koenighofer and Scott Niekum and Ufuk Topcu},
 title={{Safe Reinforcement Learning via Shielding}}, year={2017},
 howpublished={arXiv:1708.08611，预印本}, note={\url{https://arxiv.org/abs/1708.08611}}}
@misc{sentry,
 author={Changxiu Ji and Amy Lu and Qizheng Zhang and Kunle Olukotun},
 title={{Sentry: Learning to Recover from LLM Agent Failures at Test Time}}, year={2026},
 howpublished={arXiv:2610.02994v1，预印本}, note={\url{https://arxiv.org/abs/2610.02994}}}
@misc{cache, author={{Anthropic}}, title={Prompt caching},
 howpublished={Claude Platform Docs}, note={无日期；访问日期：2026-11-09。\url{https://platform.claude.com/docs/en/build-with-claude/prompt-caching}}}
''')


if __name__=='__main__':build()
