from outbox import gmail


def clean(source, mode="desktop"):
    return gmail.sanitize(source, mode=mode, seed="1")


PREFIX = gmail.class_prefix("1")


def test_scripts_forms_of_active_content_and_head_noise_are_removed():
    result = clean(
        '<html><head><title>T</title><link rel="stylesheet" href="x.css"><script>alert(1)</script></head>'
        '<body onload="x()"><p onclick="y()">Hi</p><script>bad()</script><iframe src="x"></iframe>'
        '<svg><circle/></svg><a href="javascript:alert(1)">j</a></body></html>'
    )
    assert "script" not in result.html.lower()
    assert "alert" not in result.html
    assert "onclick" not in result.html and "onload" not in result.html
    assert "<iframe" not in result.html and "<svg" not in result.html and "<link" not in result.html
    assert "<p>Hi</p>" in result.html
    assert 'href="javascript' not in result.html


def test_body_becomes_a_div_with_supported_attributes():
    result = clean('<body bgcolor="#eeeeee" style="color:#111" class="x"><p>Hi</p></body>')
    assert result.html.startswith('<div class="gmail-body" style="background-color:#eeeeee;color:#111">')


def test_style_in_head_is_kept_scoped_and_prefixed_but_body_style_is_dropped():
    result = clean(
        "<html><head><style>.card{color:red} #hero{padding:4px} body{margin:0}</style></head>"
        '<body><style>.late{color:blue}</style><div class="card" id="hero">x</div></body></html>'
    )
    css = "".join(result.styles)
    assert f".gmail-message .{PREFIX}card{{color:red}}" in css
    assert f".gmail-message #{PREFIX}hero{{padding:4px}}" in css
    assert ".gmail-message .gmail-body{margin:0}" in css
    assert "late" not in css
    assert f'class="{PREFIX}card"' in result.html and f'id="{PREFIX}hero"' in result.html
    assert "<style> inside <body> removed" in result.notes


def test_unsupported_css_is_removed_inline_and_in_style_blocks():
    result = clean(
        '<head><style>.a{position:absolute;color:red;box-shadow:0 0 1px #000;--x:1}'
        '.a:first-child{color:blue} .b::before{content:"x"} .c:hover{color:green}'
        '@font-face{font-family:X;src:url(x.woff)} @import url(x.css);'
        '@media (prefers-color-scheme: dark){.a{color:white}} @media (max-width:600px){.a{width:100%}}</style></head>'
        '<div style="position:fixed;top:0;margin:-4px 0 0;color:#333;Z-INDEX:3">x</div>'
    )
    css = "".join(result.styles)
    assert "position" not in css and "box-shadow" not in css and "--x" not in css
    assert f".{PREFIX}a{{color:red}}" in css
    assert "first-child" not in css and "before" not in css
    assert f".{PREFIX}c:hover{{color:green}}" in css
    assert "font-face" not in css and "import" not in css and "prefers-color-scheme" not in css
    assert f"@media (max-width:600px){{.gmail-message .{PREFIX}a{{width:100%}}}}" in css
    assert 'style="color:#333"' in result.html


def test_mobile_drops_hover():
    result = clean("<head><style>.c:hover{color:green}.d{color:red}</style></head>", mode="mobile")
    css = "".join(result.styles)
    assert "hover" not in css and "color:red" in css


def test_modern_color_in_style_attribute_drops_all_inline_styles():
    result = clean('<p style="color: rgb(0 128 0); font-size:20px">x</p>')
    assert "<p>x</p>" in result.html


def test_css_syntax_error_drops_the_whole_style_block():
    result = clean("<head><style>.a{color:red} .b{color:blue</style></head><p>x</p>")
    assert result.styles == []


def test_style_over_16kb_is_dropped():
    big = ".a{color:red}" * 1400
    result = clean(f"<head><style>{big}</style></head><p>x</p>")
    assert result.styles == []


def test_links_are_forced_to_open_in_a_new_tab():
    result = clean('<a href="https://example.com/x" target="_self">x</a>')
    assert '<a href="https://example.com/x" target="_blank" rel="noopener noreferrer">x</a>' in result.html


def test_media_elements_become_u_and_images_lose_srcset_and_base64():
    result = clean(
        '<picture><source srcset="a.webp"><img src="a.png" srcset="a2.png 2x" loading="lazy"></picture>'
        '<img src="data:image/png;base64,AAAA"><video src="v.mp4">fallback</video>'
    )
    assert '<u><u></u><img src="a.png"></u>' in result.html
    assert "data:image" not in result.html
    assert "<u>fallback</u>" in result.html


def test_messages_over_102kb_are_clipped():
    source = "<p>" + ("x" * 1000 + "</p><p>") * 120 + "end</p>"
    result = clean(source)
    assert result.clipped
    assert "[Message clipped]" in result.html
    assert "View entire message" in result.html
    assert "end" not in result.html
    assert len(result.html.encode()) < 110 * 1024


def test_document_includes_gmail_defaults_and_dark_mode():
    result = clean("<p>x</p>")
    page = gmail.document(result, mode="desktop")
    assert "font-family:Arial,Helvetica,sans-serif;font-size:small" in page
    assert "#1155cc" in page
    assert "invert(1)" in gmail.document(result, mode="dark")
