import pypdf
from pathlib import Path

current_dir = Path(__file__).parent
pdf_files = sorted(current_dir.glob("*.pdf"))

writer = pypdf.PdfWriter()

for pdf in pdf_files:
    writer.append(pdf)

output_name = f"{pdf_files[0].stem}-{pdf_files[-1].stem}.pdf"
writer.write(output_name)
writer.close()