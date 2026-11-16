"""Build a neutral local user guide without corporate screenshots."""
from pathlib import Path
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

ROOT = Path(__file__).resolve().parents[1]
font = next(p for p in [Path('C:/Windows/Fonts/arial.ttf'), Path('/usr/share/fonts/dejavu/DejaVuSans.ttf'), Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')] if p.exists())
pdfmetrics.registerFont(TTFont('Guide', str(font)))
styles = getSampleStyleSheet()
for style in styles.byName.values():
    style.fontName = 'Guide'
    style.leading = 16
sections = [
 ('pdf bars', 'Руководство пользователя. Приложение обрабатывает документы локально; фоновой отправки статистики нет.'),
 ('Страницы документа', 'Нажмите «Открыть» или перетащите PDF и изображения в окно редактора. Новые файлы добавляются к открытому документу. Слева доступны миниатюры: выбирайте страницы, перемещайте их и открывайте контекстное меню правой кнопкой мыши для удаления, поворота и сохранения выбранных страниц. Кнопка «Все страницы» включает обзор миниатюр.'),
 ('Просмотр и поиск', 'Настройте масштаб по ширине или по странице. Для сканов нажмите «Распознать текст», выбрав язык. Введите запрос в поле поиска. Режим чтения скрывает инструменты правки; кнопка выхода возвращает редактор.'),
 ('Правки и замена слова', 'Выберите инструмент текста или «Заменить слово» и нужное место страницы. Укажите текст, шрифт, размер, цвет, жирный, курсив и подчёркивание. Эффект сканирования можно отключить. Положение слова корректируется по X и Y. Esc снимает выделение, кнопки отмены и повтора возвращают предыдущие правки.'),
 ('Области и рисунки', 'Выделенную область можно копировать и вставлять с сохранением масштаба. «Закрыть область» маскирует фрагмент выбранным цветом; цвет можно изменить после размещения. Доступны карандаш, линия, стрелка и квадратный ластик. Закрытие области является визуальной маской, а не гарантированным безопасным удалением исходных данных.'),
 ('Подписи', 'Добавьте изображение подписи, при необходимости удалите фон и усильте контраст. Разместите подпись на странице, измените её размер. Это изображение подписи, а не квалифицированная электронная подпись.'),
 ('Обрезка и выравнивание', 'Выберите область обрезки и примените действие. Сохранённый лист уменьшится до выбранной области. Отмена возвращает прежнюю страницу. Длинные страницы можно разбивать на A4, A3 или A5; наклон корректируется автоматически по тексту или вручную.'),
 ('Преобразование и сжатие', 'В разделе «Документы» выберите направление конвертации. Для PDF → DOCX включите предварительное распознавание, при необходимости таблицы и сохранение графики. Результат представляет редактируемый текст, но сложная вёрстка требует проверки. Для DOCX/DOC → PDF программа выбирает доступный Word (Windows), Р7/ONLYOFFICE, LibreOffice или OpenOffice. При ошибке первого конвертера пробуется следующий; офисные программы устанавливаются отдельно. Отдельные вкладки преобразуют изображения в PDF и PDF в изображения. В «Сжатии» выберите оптимальный или максимальный режим; если уменьшение невозможно, программа предупредит об этом.'),
 ('Сохранение', 'Нажмите «Сохранить как» и выберите формат и новое имя. Проверьте результат перед передачей. Редактирование PDF/A может нарушить архивное соответствие. Всегда храните исходный документ.'),
 ('Язык интерфейса', 'В разделе «О программе» выберите язык интерфейса: как в системе, русский или English. Язык сохранится и применится после перезапуска; открытые документы не закрываются. По умолчанию русский выбирается только для русской системы, для остальных систем используется английский. Язык OCR выбирается отдельно и не зависит от языка интерфейса.'),
]
english = [
 ('pdf bars', 'User guide. Documents and OCR are processed locally; the application does not collect or send usage statistics.'),
 ('Pages and documents', 'Click Open or drop PDFs and images into the editor. Additional files are appended to the open document. Select pages in the left thumbnail pane, drag them to reorder, and right-click to save, rotate, duplicate or delete selected pages. Ctrl selects multiple pages; Shift selects a range. All pages opens the thumbnail grid.'),
 ('Reading and search', 'Choose Fit width or fit the entire Page. For scans, choose the OCR language and click Recognize text, then enter a search query. Reading mode hides editing tools; Exit returns to editing.'),
 ('Text and word replacement', 'Choose Text or Replace word and click the appropriate area. Enter the text and adjust its font, size, color, bold, italic and underline settings. The scan effect is optional. Adjust X and Y to move the replacement; settings can be saved as a preset. The eyedropper samples a color from the document. Esc clears selection. Undo and Redo restore previous edits.'),
 ('Areas and drawing', 'Select an area and use Ctrl+C and Ctrl+V to copy and paste it at its original scale. Cover area adds a colored mask; its color can be changed later. Pencil, line, arrow and square eraser tools are available. Covering an area is a visual mask, not guaranteed secure removal of the underlying data.'),
 ('Signatures', 'Import a signature image into the library. Remove its background and enhance faint strokes if needed. Place the signature, then move and resize it. This is an image of a signature, not a cryptographic digital signature.'),
 ('Crop, align and split', 'Select the Crop tool and draw a box around the area to keep, then Apply crop. The saved page dimensions match the selected area. Undo restores the original page. Long pages can be split into A4, A3 or A5 sheets. Align page detects text skew and also allows manual angle adjustment.'),
 ('Conversion and compression', 'In Documents, choose a conversion direction. For PDF to DOCX, enable Recognize text first, and choose Recognize tables and Keep graphics as needed. The output is editable text, but complex layouts require inspection. DOCX/DOC to PDF automatically selects installed Word on Windows, R7/ONLYOFFICE, LibreOffice or OpenOffice, and tries the next available converter if the first one fails. Office applications are installed separately. Dedicated tabs convert images to PDF and PDF to images. Compression offers Optimal and Maximum modes; the application warns if it cannot reduce the file size further.'),
 ('Saving and printing', 'Click Save as and choose a format and a new filename. Print opens the print settings for the current document. Check the result before sharing it and always keep the original. Editing PDF/A does not guarantee continued archival compliance.'),
 ('Interface language', 'Open About and choose System default, Russian or English. The preference is saved and applied after restarting the application, without closing your current document. Russian is selected automatically only on Russian systems; all other systems default to English. The interface language is independent of the OCR language.'),
 ('Installation on Windows', 'The installer installs for all users in Program Files by default and requests administrator approval once. Running the installed application does not require administrator rights; each user has separate settings. Use /CURRENTUSER for a personal installation without administrator rights, in LocalAppData/Programs. Do not grant all users permission to modify application binaries.'),
]
for filename, content in [('PDF_Bars_User_Guide.pdf',sections), ('PDF_Bars_User_Guide_en.pdf',english)]:
    story=[]
    for title, text in content:
        story += [Paragraph(title, styles['Heading1']), Paragraph(text, styles['BodyText']), Spacer(1,12)]
    target=ROOT/'docs'/filename; target.parent.mkdir(exist_ok=True)
    SimpleDocTemplate(str(target), title='pdf bars', author='pdf bars contributors').build(story)
