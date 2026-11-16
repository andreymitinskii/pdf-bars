
from .i18n import tr
"""Open actual PDFs or convert supported non-PDF documents without guessing by suffix."""
from contextlib import contextmanager

from .user_errors import UserNotice, input_path


@contextmanager
def open_pdf_input(source):
    import fitz

    path = input_path(source)
    try:
        document = fitz.open(str(path))
    except fitz.FileNotFoundError:
        input_path(path)
        raise
    except (fitz.FileDataError, fitz.EmptyFileError) as error:
        raise UserNotice(tr("Не удалось прочитать документ"),
                         tr(f"Файл пуст, повреждён или его формат не поддерживается:\n{path}\n\n"
                         "Откройте его в исходной программе и сохраните новую копию в PDF либо получите файл повторно.")) from error
    with document:
        if document.needs_pass:
            raise UserNotice(tr("Документ защищён паролем"),
                             tr("Откройте документ с разрешённым вам паролем в просмотрщике и сохраните "
                             "разрешённую для редактирования копию PDF. Затем добавьте её в pdf bars."))
        if document.is_pdf:
            yield document
        else:
            with fitz.open(stream=document.convert_to_pdf(), filetype="pdf") as converted:
                yield converted
