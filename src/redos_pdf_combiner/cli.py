from __future__ import annotations

from .i18n import tr

import argparse
from pathlib import Path

from . import __version__
from .core import (
    add_text_watermark,
    compress_pdf,
    decrypt_pdf,
    delete_pages,
    encrypt_pdf,
    extract_pages,
    merge_pdfs,
    rotate_pages,
    split_pdf_each_page,
    split_pdf_every,
)
from .converters import docx_to_pdf, images_to_individual_pdfs, images_to_pdf, pdf_to_docx, pdf_to_images
from .layout_ocr import pdfs_to_layout_ocr_docx


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pdf-bars", description="PDF combiner and utility toolkit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    merge = sub.add_parser("merge", help=tr("Объединить PDF"))
    merge.add_argument("inputs", nargs="+")
    merge.add_argument("-o", "--output", required=True)

    extract = sub.add_parser("extract", help=tr("Извлечь страницы"))
    extract.add_argument("input")
    extract.add_argument("-p", "--pages", required=True)
    extract.add_argument("-o", "--output", required=True)

    delete = sub.add_parser("delete", help=tr("Удалить страницы"))
    delete.add_argument("input")
    delete.add_argument("-p", "--pages", required=True)
    delete.add_argument("-o", "--output", required=True)

    rotate = sub.add_parser("rotate", help=tr("Повернуть страницы"))
    rotate.add_argument("input")
    rotate.add_argument("-p", "--pages", default="all")
    rotate.add_argument("-d", "--degrees", type=int, default=90)
    rotate.add_argument("-o", "--output", required=True)

    split = sub.add_parser("split", help=tr("Разделить PDF"))
    split.add_argument("input")
    split.add_argument("-o", "--output-dir", required=True)
    split.add_argument("--every", type=int, default=1, help=tr("Страниц в одном файле"))

    compress = sub.add_parser("compress", help=tr("Сжать PDF"))
    compress.add_argument("input")
    compress.add_argument("-q", "--quality", default="ebook", choices=["screen", "ebook", "printer", "prepress", "default"])
    compress.add_argument("--preset", choices=["maximum", "medium", "minimum"], help=tr("Пресет сжатия: maximum, medium или minimum"))
    compress.add_argument("--target-mb", type=float, help=tr("Целевой размер результата в МБ"))
    compress.add_argument("--color-mode", default="color", choices=["color", "grayscale", "monochrome"], help=tr("Цветной, оттенки серого или 1-битный режим"))
    compress.add_argument("-o", "--output", required=True)

    encrypt = sub.add_parser("encrypt", help=tr("Поставить пароль"))
    encrypt.add_argument("input")
    encrypt.add_argument("-p", "--password", required=True)
    encrypt.add_argument("--owner-password")
    encrypt.add_argument("-o", "--output", required=True)

    decrypt = sub.add_parser("decrypt", help=tr("Снять пароль при наличии пароля"))
    decrypt.add_argument("input")
    decrypt.add_argument("-p", "--password", required=True)
    decrypt.add_argument("-o", "--output", required=True)

    watermark = sub.add_parser("watermark", help=tr("Добавить текстовый водяной знак"))
    watermark.add_argument("input")
    watermark.add_argument("-t", "--text", required=True)
    watermark.add_argument("-p", "--pages", default="all")
    watermark.add_argument("--font-size", type=int, default=48)
    watermark.add_argument("--opacity", type=float, default=0.18)
    watermark.add_argument("-o", "--output", required=True)

    images = sub.add_parser("images", help=tr("Создать PDF из изображений, включая WEBP"))
    images.add_argument("inputs", nargs="+")
    images.add_argument("-o", "--output", help=tr("Один общий PDF"))
    images.add_argument("--output-dir", help=tr("Папка для режима --one-per-file"))
    images.add_argument("--one-per-file", action="store_true", help=tr("Создать отдельный PDF для каждого изображения"))
    images.add_argument("--dpi", type=int, default=150)

    pdf_images = sub.add_parser("pdf2images", help=tr("Преобразовать страницы PDF в изображения"))
    pdf_images.add_argument("input")
    pdf_images.add_argument("-o", "--output-dir", required=True)
    pdf_images.add_argument("--format", default="jpg", choices=["jpg", "png", "webp", "tiff"])
    pdf_images.add_argument("--dpi", type=int, default=150)

    docx = sub.add_parser("docx2pdf", help=tr("Пакетно конвертировать DOCX/DOC в PDF через установленный офисный конвертер"))
    docx.add_argument("inputs", nargs="+")
    docx.add_argument("-o", "--output-dir", required=True)

    pdf_docx = sub.add_parser("pdf2docx", help=tr("Пакетно конвертировать PDF в DOCX через R7-Office или встроенное OCR"))
    pdf_docx.add_argument("inputs", nargs="+")
    pdf_docx.add_argument("-o", "--output-dir", required=True)
    pdf_docx.add_argument("--ocr", action="store_true", help=tr("Создать редактируемый DOCX с распознанным текстом"))
    pdf_docx.add_argument(
        "--ocr-graphics",
        action="store_true",
        help=tr("При OCR дополнительно сохранить логотипы, линии, печати и другую графику"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "merge":
        result = merge_pdfs(args.inputs, args.output)
    elif args.command == "extract":
        result = extract_pages(args.input, args.pages, args.output)
    elif args.command == "delete":
        result = delete_pages(args.input, args.pages, args.output)
    elif args.command == "rotate":
        result = rotate_pages(args.input, args.pages, args.degrees, args.output)
    elif args.command == "split":
        outputs = split_pdf_each_page(args.input, args.output_dir) if args.every == 1 else split_pdf_every(args.input, args.output_dir, args.every)
        print(tr(f"Создано файлов: {len(outputs)}"))
        return 0
    elif args.command == "compress":
        if args.target_mb is not None:
            from .core import compress_pdf_to_size

            target_result = compress_pdf_to_size(args.input, args.output, args.target_mb, args.color_mode)
            print(target_result.path)
            print(tr(f"Размер: {target_result.actual_mb:.2f} МБ; цель: {target_result.target_mb:.2f} МБ"))
            return 0 if target_result.achieved else 3
        if args.preset:
            from .core import compress_pdf_preset

            result = compress_pdf_preset(args.input, args.output, args.preset, args.color_mode)
        else:
            result = compress_pdf(args.input, args.output, args.quality)
    elif args.command == "encrypt":
        result = encrypt_pdf(args.input, args.output, args.password, args.owner_password)
    elif args.command == "decrypt":
        result = decrypt_pdf(args.input, args.output, args.password)
    elif args.command == "watermark":
        result = add_text_watermark(args.input, args.output, args.text, args.pages, args.font_size, args.opacity)
    elif args.command == "images":
        if args.one_per_file:
            if not args.output_dir:
                parser.error(tr("Для --one-per-file нужна папка --output-dir"))
                return 2
            outputs = images_to_individual_pdfs(args.inputs, args.output_dir, args.dpi)
            print(tr(f"Создано файлов: {len(outputs)}"))
            return 0
        if not args.output:
            parser.error(tr("Для общего PDF нужен параметр -o/--output"))
            return 2
        result = images_to_pdf(args.inputs, args.output, args.dpi)
    elif args.command == "docx2pdf":
        outputs = docx_to_pdf(args.inputs, args.output_dir)
        print(tr(f"Создано файлов: {len(outputs)}"))
        return 0
    elif args.command == "pdf2docx":
        outputs = (
            pdfs_to_layout_ocr_docx(
                args.inputs,
                args.output_dir,
                preserve_graphics=args.ocr_graphics,
            )
            if args.ocr
            else pdf_to_docx(args.inputs, args.output_dir)
        )
        print(tr(f"Создано файлов: {len(outputs)}"))
        return 0
    elif args.command == "pdf2images":
        outputs = pdf_to_images(args.input, args.output_dir, args.format, args.dpi)
        print(tr(f"Создано файлов: {len(outputs)}"))
        return 0
    else:
        parser.error(tr("Неизвестная команда"))
        return 2

    print(Path(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
