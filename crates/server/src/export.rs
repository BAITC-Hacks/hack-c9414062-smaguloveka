// Adapted from Memora / Meetily. See THIRD_PARTY_NOTICES.md.
use printpdf::{IndirectFontRef, Mm, OffsetDateTime, PdfDocument, PdfDocumentReference, PdfLayerReference};
use sha2::{Digest,Sha256};
use std::{fs::File,io::{Cursor,Write},path::PathBuf};
use zip::{write::SimpleFileOptions,CompressionMethod,DateTime,ZipWriter};
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum BlockKind {
    Title,
    HeadingOne,
    HeadingTwo,
    Bullet,
    Numbered,
    Body,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct MarkdownBlock {
    kind: BlockKind,
    text: String,
}

fn parse_markdown(content: &str) -> Vec<MarkdownBlock> {
    content
        .lines()
        .filter_map(|raw| {
            let line = raw.trim();
            if line.is_empty() {
                return None;
            }

            let (kind, text) = if let Some(text) = line.strip_prefix("### ") {
                (BlockKind::HeadingTwo, text)
            } else if let Some(text) = line.strip_prefix("## ") {
                (BlockKind::HeadingOne, text)
            } else if let Some(text) = line.strip_prefix("# ") {
                (BlockKind::Title, text)
            } else if let Some(text) = line.strip_prefix("- ").or_else(|| line.strip_prefix("* ")) {
                (BlockKind::Bullet, text)
            } else if let Some(text) = strip_numbered_prefix(line) {
                (BlockKind::Numbered, text)
            } else {
                (BlockKind::Body, line)
            };

            let text = strip_inline_markdown(text).trim().to_string();
            (!text.is_empty()).then_some(MarkdownBlock { kind, text })
        })
        .collect()
}

fn strip_numbered_prefix(line: &str) -> Option<&str> {
    let digit_count = line
        .chars()
        .take_while(|character| character.is_ascii_digit())
        .count();
    if digit_count == 0 {
        return None;
    }
    line.get(digit_count..)?.strip_prefix(". ")
}

fn strip_inline_markdown(text: &str) -> String {
    text.replace("**", "")
        .replace("__", "")
        .replace('`', "")
        .replace("~~", "")
}

/* -------------------------------------------------------------------------- */
/* DOCX                                                                        */

pub fn render_docx(document_title: &str, content: &str) -> Result<Vec<u8>, String> {
    let blocks = parse_markdown(content);
    let mut document_body = String::new();
    for block in blocks {
        document_body.push_str(&docx_paragraph(&block));
    }

    let document_xml = format!(
        r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>{document_body}<w:sectPr>
    <w:footerReference w:type="default" r:id="rId3"/>
    <w:pgSz w:w="12240" w:h="15840"/>
    <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/>
    <w:cols w:space="708"/>
  </w:sectPr></w:body>
</w:document>"#
    );

    let files = [
        ("[Content_Types].xml", content_types_xml()),
        ("_rels/.rels", package_relationships_xml()),
        ("docProps/core.xml", core_properties_xml(document_title)),
        ("docProps/app.xml", app_properties_xml()),
        ("word/document.xml", document_xml),
        ("word/styles.xml", styles_xml()),
        ("word/numbering.xml", numbering_xml()),
        ("word/footer1.xml", footer_xml()),
        ("word/_rels/document.xml.rels", document_relationships_xml()),
    ];

    let cursor = Cursor::new(Vec::new());
    let mut archive = ZipWriter::new(cursor);
    let modified = DateTime::from_date_and_time(1980, 1, 1, 0, 0, 0)
        .map_err(|error| format!("Не удалось подготовить DOCX: {error}"))?;
    let options = SimpleFileOptions::default()
        .compression_method(CompressionMethod::Deflated)
        .last_modified_time(modified);

    for (name, xml) in files {
        archive
            .start_file(name, options)
            .map_err(|error| format!("Не удалось подготовить DOCX: {error}"))?;
        archive
            .write_all(xml.as_bytes())
            .map_err(|error| format!("Не удалось подготовить DOCX: {error}"))?;
    }

    archive
        .finish()
        .map(|cursor| cursor.into_inner())
        .map_err(|error| format!("Не удалось завершить DOCX: {error}"))
}

fn docx_paragraph(block: &MarkdownBlock) -> String {
    let text = escape_xml(&block.text);
    match block.kind {
        BlockKind::Title => format!(
            r#"<w:p><w:pPr><w:pStyle w:val="Title"/><w:keepNext/></w:pPr><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>"#
        ),
        BlockKind::HeadingOne => format!(
            r#"<w:p><w:pPr><w:pStyle w:val="Heading1"/><w:keepNext/></w:pPr><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>"#
        ),
        BlockKind::HeadingTwo => format!(
            r#"<w:p><w:pPr><w:pStyle w:val="Heading2"/><w:keepNext/></w:pPr><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>"#
        ),
        BlockKind::Bullet => numbered_docx_paragraph(&text, 1),
        BlockKind::Numbered => numbered_docx_paragraph(&text, 2),
        BlockKind::Body => format!(
            r#"<w:p><w:pPr><w:widowControl/></w:pPr><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>"#
        ),
    }
}

fn numbered_docx_paragraph(text: &str, number_id: u8) -> String {
    format!(
        r#"<w:p><w:pPr><w:pStyle w:val="ListParagraph"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="{number_id}"/></w:numPr><w:widowControl/></w:pPr><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>"#
    )
}

fn escape_xml(text: &str) -> String {
    let mut escaped = String::with_capacity(text.len());
    for character in text.chars() {
        if character.is_control() && !matches!(character, '\t' | '\n' | '\r') {
            continue;
        }
        match character {
            '&' => escaped.push_str("&amp;"),
            '<' => escaped.push_str("&lt;"),
            '>' => escaped.push_str("&gt;"),
            '"' => escaped.push_str("&quot;"),
            '\'' => escaped.push_str("&apos;"),
            _ => escaped.push(character),
        }
    }
    escaped
}

fn content_types_xml() -> String {
    r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
  <Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"#.to_string()
}

fn package_relationships_xml() -> String {
    r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"#.to_string()
}

fn document_relationships_xml() -> String {
    r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
</Relationships>"#.to_string()
}

fn core_properties_xml(title: &str) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>{}</dc:title><dc:creator>AI Hatshy</dc:creator>
</cp:coreProperties>"#,
        escape_xml(title)
    )
}

fn app_properties_xml() -> String {
    r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>AI Hatshy</Application><AppVersion>0.4.2</AppVersion>
</Properties>"#.to_string()
}

fn styles_xml() -> String {
    r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Arial" w:cs="Arial"/><w:sz w:val="22"/><w:szCs w:val="22"/><w:color w:val="202428"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="264" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="120" w:line="264" w:lineRule="auto"/></w:pPr><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Arial" w:cs="Arial"/><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:before="0" w:after="160"/><w:keepNext/></w:pPr><w:rPr><w:b/><w:bCs/><w:sz w:val="46"/><w:szCs w:val="46"/><w:color w:val="111827"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:before="320" w:after="160"/><w:keepNext/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:bCs/><w:sz w:val="32"/><w:szCs w:val="32"/><w:color w:val="2E74B5"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:before="240" w:after="120"/><w:keepNext/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:bCs/><w:sz w:val="26"/><w:szCs w:val="26"/><w:color w:val="2E74B5"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:contextualSpacing/></w:pPr></w:style>
</w:styles>"#.to_string()
}

fn numbering_xml() -> String {
    r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="singleLevel"/><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/><w:lvlJc w:val="left"/><w:pPr><w:tabs><w:tab w:val="num" w:pos="720"/></w:tabs><w:ind w:left="720" w:hanging="360"/><w:spacing w:after="80" w:line="280" w:lineRule="auto"/></w:pPr><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/></w:rPr></w:lvl></w:abstractNum>
  <w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="singleLevel"/><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:lvlJc w:val="left"/><w:pPr><w:tabs><w:tab w:val="num" w:pos="720"/></w:tabs><w:ind w:left="720" w:hanging="360"/><w:spacing w:after="80" w:line="280" w:lineRule="auto"/></w:pPr></w:lvl></w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num><w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
</w:numbering>"#.to_string()
}

fn footer_xml() -> String {
    r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:pPr><w:jc w:val="right"/><w:spacing w:before="80"/></w:pPr><w:r><w:rPr><w:color w:val="7A818A"/><w:sz w:val="18"/></w:rPr><w:t>AI Hatshy · </w:t></w:r><w:fldSimple w:instr=" PAGE "><w:r><w:rPr><w:color w:val="7A818A"/><w:sz w:val="18"/></w:rPr><w:t>1</w:t></w:r></w:fldSimple></w:p></w:ftr>"#.to_string()
}

/* -------------------------------------------------------------------------- */
/* PDF                                                                         */

const LETTER_WIDTH_MM: f32 = 215.9;
const LETTER_HEIGHT_MM: f32 = 279.4;
const PDF_LEFT_MM: f32 = 25.4;
const PDF_RIGHT_MM: f32 = 25.4;
const PDF_TOP_MM: f32 = 25.4;
const PDF_BOTTOM_MM: f32 = 22.0;

pub fn render_pdf(document_title: &str, content: &str) -> Result<Vec<u8>, String> {
    let font_path = find_pdf_font().ok_or_else(|| {
        "Не найден системный шрифт с поддержкой кириллицы для PDF. Установите Arial, DejaVu Sans или Liberation Sans.".to_string()
    })?;
    let font_file = File::open(&font_path).map_err(|error| {
        format!(
            "Не удалось открыть шрифт PDF {}: {error}",
            font_path.display()
        )
    })?;

    // printpdf inserts the title into XMP without XML escaping. Feed the XMP-
    // safe spelling and restore the human title in the PDF Info dictionary
    // after serialization.
    let metadata_title = escape_xml(document_title);
    let (document, first_page, first_layer) = PdfDocument::new(
        &metadata_title,
        Mm(LETTER_WIDTH_MM),
        Mm(LETTER_HEIGHT_MM),
        "Content",
    );
    let stable_id = stable_document_id(document_title, content);
    // Stable metadata values avoid leaking the export time into a document.
    let epoch = OffsetDateTime::UNIX_EPOCH;
    let document = document
        .with_author("AI Hatshy")
        .with_creator("AI Hatshy")
        .with_producer("AI Hatshy")
        .with_creation_date(epoch)
        .with_metadata_date(epoch)
        .with_mod_date(epoch)
        .with_document_id(stable_id.clone());
    let font = document
        .add_external_font(font_file)
        .map_err(|error| format!("Не удалось встроить шрифт в PDF: {error}"))?;
    let layer = document.get_page(first_page).get_layer(first_layer);
    let mut layout = PdfLayout::new(document, font, layer);

    for block in parse_markdown(content) {
        layout.add_block(block);
    }

    let bytes = layout
        .document
        .save_to_bytes()
        .map_err(|error| format!("Не удалось сформировать PDF: {error}"))?;

    // printpdf creates a random trailer ID internally even when XMP metadata
    // has a fixed ID. Normalize it and serialize once more so equal previews
    // produce byte-for-byte equal PDFs.
    let mut pdf = printpdf::lopdf::Document::load_mem(&bytes)
        .map_err(|error| format!("Не удалось проверить PDF: {error}"))?;
    normalize_pdf_metadata(&mut pdf, document_title, &stable_id)?;
    let id = printpdf::lopdf::Object::String(
        stable_id.into_bytes(),
        printpdf::lopdf::StringFormat::Hexadecimal,
    );
    pdf.trailer
        .set("ID", printpdf::lopdf::Object::Array(vec![id.clone(), id]));
    let mut deterministic = Vec::new();
    pdf.save_to(&mut deterministic)
        .map_err(|error| format!("Не удалось завершить PDF: {error}"))?;
    Ok(deterministic)
}

fn normalize_pdf_metadata(
    pdf: &mut printpdf::lopdf::Document,
    document_title: &str,
    stable_id: &str,
) -> Result<(), String> {
    if let Ok(info_reference) = pdf
        .trailer
        .get(b"Info")
        .and_then(|value| value.as_reference())
    {
        if let Ok(info) = pdf
            .get_object_mut(info_reference)
            .and_then(|value| value.as_dict_mut())
        {
            info.set("Title", pdf_unicode_string(document_title));
        }
    }

    for object in pdf.objects.values_mut() {
        let printpdf::lopdf::Object::Stream(stream) = object else {
            continue;
        };
        let Ok(plain) = stream.decompressed_content() else {
            continue;
        };
        let Ok(mut xml) = String::from_utf8(plain) else {
            continue;
        };
        if !xml.contains("<xmpMM:InstanceID>") {
            continue;
        }
        replace_xmp_uuid(&mut xml, "DocumentID", stable_id)?;
        replace_xmp_uuid(&mut xml, "InstanceID", stable_id)?;
        stream.set_plain_content(xml.into_bytes());
    }
    Ok(())
}

fn pdf_unicode_string(value: &str) -> printpdf::lopdf::Object {
    let mut bytes = Vec::with_capacity(2 + value.len() * 2);
    bytes.extend_from_slice(&[0xFE, 0xFF]);
    for unit in value.encode_utf16() {
        bytes.extend_from_slice(&unit.to_be_bytes());
    }
    printpdf::lopdf::Object::String(bytes, printpdf::lopdf::StringFormat::Literal)
}

fn replace_xmp_uuid(xml: &mut String, element: &str, stable_id: &str) -> Result<(), String> {
    let opening = format!("<xmpMM:{element}>uuid:");
    let closing = format!("</xmpMM:{element}>");
    let start = xml
        .find(&opening)
        .map(|index| index + opening.len())
        .ok_or_else(|| format!("Не удалось нормализовать PDF: нет {element}"))?;
    let end = xml[start..]
        .find(&closing)
        .map(|index| start + index)
        .ok_or_else(|| format!("Не удалось нормализовать PDF: повреждён {element}"))?;
    xml.replace_range(start..end, stable_id);
    Ok(())
}

fn stable_document_id(document_title: &str, content: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(document_title.as_bytes());
    hasher.update([0]);
    hasher.update(content.as_bytes());
    hasher
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

struct PdfLayout {
    document: PdfDocumentReference,
    font: IndirectFontRef,
    layer: PdfLayerReference,
    y_mm: f32,
    page_number: usize,
}

impl PdfLayout {
    fn new(
        document: PdfDocumentReference,
        font: IndirectFontRef,
        layer: PdfLayerReference,
    ) -> Self {
        let layout = Self {
            document,
            font,
            layer,
            y_mm: LETTER_HEIGHT_MM - PDF_TOP_MM,
            page_number: 1,
        };
        layout.add_footer();
        layout
    }

    fn add_block(&mut self, block: MarkdownBlock) {
        let (size, line_height, before, after, indent, prefix) = match block.kind {
            BlockKind::Title => (21.0, 9.5, 0.0, 5.0, 0.0, ""),
            BlockKind::HeadingOne => (15.0, 7.5, 5.0, 2.5, 0.0, ""),
            BlockKind::HeadingTwo => (12.5, 6.5, 4.0, 2.0, 0.0, ""),
            BlockKind::Bullet => (10.5, 5.5, 0.0, 1.2, 5.0, "- "),
            BlockKind::Numbered => (10.5, 5.5, 0.0, 1.2, 5.0, "1. "),
            BlockKind::Body => (10.5, 5.5, 0.0, 2.0, 0.0, ""),
        };
        self.y_mm -= before;

        let available_mm = LETTER_WIDTH_MM - PDF_LEFT_MM - PDF_RIGHT_MM - indent;
        let average_character_mm = (size * 0.352_778) * 0.52;
        let max_characters = (available_mm / average_character_mm).floor().max(16.0) as usize;
        let lines = wrap_words(
            &block.text,
            max_characters.saturating_sub(prefix.chars().count()),
        );

        for (index, line) in lines.iter().enumerate() {
            self.ensure_space(line_height);
            let rendered = if index == 0 && !prefix.is_empty() {
                format!("{prefix}{line}")
            } else {
                line.clone()
            };
            self.layer.use_text(
                rendered,
                size,
                Mm(PDF_LEFT_MM + indent),
                Mm(self.y_mm),
                &self.font,
            );
            self.y_mm -= line_height;
        }
        self.y_mm -= after;
    }

    fn ensure_space(&mut self, needed_mm: f32) {
        if self.y_mm - needed_mm >= PDF_BOTTOM_MM {
            return;
        }
        let (page, layer) = self.document.add_page(
            Mm(LETTER_WIDTH_MM),
            Mm(LETTER_HEIGHT_MM),
            format!("Content {}", self.page_number + 1),
        );
        self.page_number += 1;
        self.layer = self.document.get_page(page).get_layer(layer);
        self.y_mm = LETTER_HEIGHT_MM - PDF_TOP_MM;
        self.add_footer();
    }

    fn add_footer(&self) {
        self.layer.use_text(
            format!("AI Hatshy · {}", self.page_number),
            8.0,
            Mm(LETTER_WIDTH_MM - PDF_RIGHT_MM - 22.0),
            Mm(12.0),
            &self.font,
        );
    }
}

fn wrap_words(text: &str, max_characters: usize) -> Vec<String> {
    let max_characters = max_characters.max(1);
    let mut lines = Vec::new();
    let mut current = String::new();

    for word in text.split_whitespace() {
        if word.chars().count() > max_characters {
            if !current.is_empty() {
                lines.push(std::mem::take(&mut current));
            }
            let mut chunk = String::new();
            for character in word.chars() {
                chunk.push(character);
                if chunk.chars().count() == max_characters {
                    lines.push(std::mem::take(&mut chunk));
                }
            }
            current = chunk;
            continue;
        }

        let separator = usize::from(!current.is_empty());
        if current.chars().count() + separator + word.chars().count() > max_characters {
            lines.push(std::mem::take(&mut current));
        }
        if !current.is_empty() {
            current.push(' ');
        }
        current.push_str(word);
    }

    if !current.is_empty() {
        lines.push(current);
    }
    if lines.is_empty() {
        lines.push(String::new());
    }
    lines
}

fn find_pdf_font() -> Option<PathBuf> {
    if let Some(path) = std::env::var_os("HATSHY_EXPORT_FONT").map(PathBuf::from) {
        if path.is_file() {
            return Some(path);
        }
    }

    #[cfg(target_os = "macos")]
    let candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial.ttf",
    ];
    #[cfg(target_os = "windows")]
    let candidates = [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
    ];
    #[cfg(target_os = "linux")]
    let candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    ];
    #[cfg(not(any(target_os = "macos", target_os = "windows", target_os = "linux")))]
    let candidates: [&str; 0] = [];

    candidates
        .into_iter()
        .map(PathBuf::from)
        .find(|candidate| candidate.is_file())
}

