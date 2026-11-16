"""Let structural UI assertions ignore localization markers, not their text."""
import ast

def source_text(path):
    text=path.read_text(encoding='utf-8')
    if path.suffix != '.py':
        return text
    tree=ast.parse(text)
    lines=text.splitlines(keepends=True); offsets=[0]
    for line in lines: offsets.append(offsets[-1]+len(line))
    def pos(node,end=False):
        row=node.end_lineno if end else node.lineno
        col=node.end_col_offset if end else node.col_offset
        return offsets[row-1]+len(lines[row-1].encode('utf-8')[:col].decode('utf-8'))
    edits=[]
    for node in ast.walk(tree):
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='tr' and len(node.args)==1:
            edits.extend([(pos(node),pos(node.args[0])),(pos(node.args[0],True),pos(node,True))])
    for start,end in sorted(edits,reverse=True): text=text[:start]+text[end:]
    return text
