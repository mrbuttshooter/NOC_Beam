"""NOC_Beam Flet (Flutter) POC -- the strongest non-Qt rival, rendered.

Pure look artifact: Dial surface with live-call card, keypad, recents.
Run:  .venv\\Scripts\\python.exe prototype\\flet_poc.py
"""
from __future__ import annotations

import flet as ft

KEYS = [
    ("1", ""), ("2", "ABC"), ("3", "DEF"),
    ("4", "GHI"), ("5", "JKL"), ("6", "MNO"),
    ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"),
    ("*", ""), ("0", "+"), ("#", ""),
]

RECENTS = [
    ("96170010600", "Answered", ft.Colors.GREEN_400, "17:06"),
    ("35796109901", "Cancelled", ft.Colors.RED_400, "10:47"),
    ("96899406599", "Not found", ft.Colors.RED_400, "11:48"),
]

INDIGO = "#5B6EE0"


def key_tile(digit: str, caption: str) -> ft.Control:
    return ft.Container(
        content=ft.Column(
            [
                ft.Text(digit, size=16, weight=ft.FontWeight.W_500),
                ft.Text(caption, size=8, color=ft.Colors.GREY_500) if caption
                else ft.Container(height=10),
            ],
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            tight=True,
        ),
        bgcolor=ft.Colors.with_opacity(0.06, ft.Colors.WHITE),
        border_radius=8,
        padding=ft.Padding(0, 6, 0, 6),
        expand=True,
        ink=True,
        on_click=lambda e: None,
        alignment=ft.Alignment.CENTER if hasattr(ft.Alignment,'CENTER') else ft.Alignment(0,0),
    )


def main(page: ft.Page):
    page.title = "NOC_Beam"
    page.theme_mode = ft.ThemeMode.DARK
    page.theme = ft.Theme(color_scheme_seed=INDIGO)
    page.window.width = 420
    page.window.height = 760
    page.padding = 0

    rail = ft.NavigationRail(
        selected_index=0,
        label_type=ft.NavigationRailLabelType.ALL,
        min_width=68,
        destinations=[
            ft.NavigationRailDestination(icon=ft.Icons.DIALPAD, label="Dial"),
            ft.NavigationRailDestination(icon=ft.Icons.PEOPLE_OUTLINE, label="Contacts"),
            ft.NavigationRailDestination(
                icon=ft.Icon(ft.Icons.HISTORY), label="History"),
        ],
        trailing=ft.IconButton(ft.Icons.SETTINGS_OUTLINED),
    )

    live_call = ft.Card(
        elevation=2,
        content=ft.Container(
            padding=14,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text("0035796109901", size=17,
                                    font_family="Cascadia Mono",
                                    weight=ft.FontWeight.W_600, expand=True),
                            ft.Container(
                                content=ft.Text("Ringing · 00:05", size=11,
                                                color=ft.Colors.AMBER_200),
                                bgcolor=ft.Colors.with_opacity(
                                    0.15, ft.Colors.AMBER),
                                border_radius=20,
                                padding=ft.Padding(10, 6, 10, 6),
                            ),
                        ]
                    ),
                    ft.Text("via Teles UK · AAA Tel — C207", size=11,
                            color=ft.Colors.GREY_500),
                    ft.Row(
                        [
                            ft.IconButton(ft.Icons.MIC_NONE, tooltip="Mute"),
                            ft.IconButton(ft.Icons.PAUSE, tooltip="Hold"),
                            ft.IconButton(ft.Icons.PHONE_FORWARDED_OUTLINED,
                                          tooltip="Transfer"),
                            ft.Container(expand=True),
                            ft.FilledButton(
                                "End call", icon=ft.Icons.CALL_END,
                                style=ft.ButtonStyle(
                                    bgcolor=ft.Colors.RED_400,
                                    color=ft.Colors.WHITE),
                            ),
                        ],
                        spacing=2,
                    ),
                ],
                spacing=6,
                tight=True,
            ),
        ),
    )

    recents = [
        ft.Container(
            content=ft.Row(
                [
                    ft.Text(num, font_family="Cascadia Mono", size=13,
                            expand=True),
                    ft.Container(
                        content=ft.Text(status, size=10, color=color),
                        border=ft.Border.all(1, ft.Colors.with_opacity(0.4, color)),
                        border_radius=20,
                        padding=ft.Padding(8, 3, 8, 3),
                    ),
                    ft.Text(when, size=11, color=ft.Colors.GREY_600),
                    ft.IconButton(ft.Icons.PHONE_OUTLINED, icon_size=16),
                ]
            ),
            padding=ft.Padding(8, 2, 8, 2),
            border_radius=8,
            ink=True,
            on_click=lambda e: None,
        )
        for num, status, color, when in RECENTS
    ]

    dial = ft.Column(
        [
            ft.Dropdown(
                value="AAA Tel — C207",
                options=[ft.dropdown.Option("AAA Tel — C207"),
                         ft.dropdown.Option("Ibasis (Premium) — C080")],
                dense=True,
            ),
            live_call,
            ft.Row(
                [
                    ft.TextField(
                        hint_text="Number or SIP URI", expand=True,
                        dense=True, text_style=ft.TextStyle(
                            font_family="Cascadia Mono"),
                    ),
                    ft.FilledButton("Call", icon=ft.Icons.PHONE),
                ],
                spacing=8,
            ),
            ft.Column(
                [
                    ft.Row([key_tile(d, c) for d, c in KEYS[r*3:r*3+3]],
                           spacing=6)
                    for r in range(4)
                ],
                spacing=6,
            ),
            ft.Row(
                [
                    ft.Text("Recent calls", weight=ft.FontWeight.W_600,
                            expand=True),
                    ft.TextButton("View all"),
                ]
            ),
            *recents,
        ],
        spacing=10,
        expand=True,
        scroll=ft.ScrollMode.AUTO,
    )

    page.add(
        ft.Row(
            [
                rail,
                ft.VerticalDivider(width=1),
                ft.Container(dial, padding=14, expand=True),
            ],
            expand=True,
            spacing=0,
        )
    )


if __name__ == "__main__":
    ft.app(target=main)
