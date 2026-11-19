import os
from nicegui import ui

# =====================================================
# ESAM ANALYTICS 360° — NUEVA VERSIÓN NICEGUI
# Pantalla inicial. Todavía no conecta con Neon.
# =====================================================

ui.page_title('ESAM Analytics 360°')
ui.dark_mode().enable()
ui.colors(primary='#3B82F6')

ui.add_head_html("""
<style>
    body {
        background: #090A0F;
        color: #F1F5F9;
        font-family: Arial, sans-serif;
    }
    .esam-card {
        background: #151A25;
        border: 1px solid #263244;
        border-radius: 14px;
        padding: 20px;
        min-width: 210px;
        flex: 1;
    }
    .esam-muted {
        color: #94A3B8;
    }
</style>
""")


@ui.page('/')
def inicio():

    # Menú lateral
    with ui.left_drawer(value=True).classes('bg-[#111827]'):
        ui.label('ESAM ANALYTICS').classes(
            'text-xl font-bold text-white'
        )
        ui.label('360° CONTROL CENTER').classes(
            'text-xs text-blue-400 mb-6'
        )

        ui.separator().classes('mb-4')

        ui.button(
            '📊  Panel General',
            on_click=lambda: ui.notify('Panel General')
        ).props('flat').classes('w-full justify-start text-white')

        ui.button(
            '🏀  Módulo Básquetbol',
            on_click=lambda: ui.notify('Módulo Básquetbol')
        ).props('flat').classes('w-full justify-start text-white')

        ui.button(
            '⚾  Módulo Béisbol',
            on_click=lambda: ui.notify('Módulo Béisbol')
        ).props('flat').classes('w-full justify-start text-white')

        ui.separator().classes('my-4')

        ui.button(
            '⚙️  Administración',
            on_click=lambda: ui.notify('Administración')
        ).props('flat').classes('w-full justify-start text-white')

        ui.button(
            '🔐  Seguridad',
            on_click=lambda: ui.notify('Seguridad')
        ).props('flat').classes('w-full justify-start text-white')

    # Área principal
    with ui.column().classes('w-full p-6 gap-5'):

        with ui.row().classes('w-full items-center justify-between'):
            with ui.column().classes('gap-1'):
                ui.label('Panel General').classes(
                    'text-3xl font-bold text-white'
                )
                ui.label(
                    'Centro de control y estadísticas de operaciones'
                ).classes('esam-muted')

            ui.badge('NUEVA VERSIÓN', color='primary')

        ui.separator()

        ui.label('Resumen de actividad').classes(
            'text-xl font-semibold text-white'
        )

        with ui.row().classes('w-full gap-4 flex-wrap'):

            with ui.card().classes('esam-card'):
                ui.label('🏀 Básquetbol').classes('esam-muted')
                ui.label('—').classes('text-3xl font-bold text-blue-400')
                ui.label('Registros').classes('esam-muted')

            with ui.card().classes('esam-card'):
                ui.label('⚾ Béisbol').classes('esam-muted')
                ui.label('—').classes('text-3xl font-bold text-green-400')
                ui.label('Registros').classes('esam-muted')

            with ui.card().classes('esam-card'):
                ui.label('👥 Operadores').classes('esam-muted')
                ui.label('—').classes('text-3xl font-bold text-purple-400')
                ui.label('Usuarios registrados').classes('esam-muted')

            with ui.card().classes('esam-card'):
                ui.label('📈 Actividad').classes('esam-muted')
                ui.label('—').classes('text-3xl font-bold text-cyan-400')
                ui.label('Resumen general').classes('esam-muted')

        with ui.card().classes('w-full bg-[#151A25] border border-[#263244]'):
            ui.label('Bienvenido a ESAM Analytics 360°').classes(
                'text-xl font-bold text-white'
            )
            ui.label(
                'Estamos preparando la nueva plataforma de control '
                'y estadísticas. Los datos aparecerán cuando '
                'conectemos la base de datos de pruebas.'
            ).classes('esam-muted')

            ui.badge('Base de datos pendiente de conexión', color='warning')


if __name__ == '__main__':
    ui.run(
        title='ESAM Analytics 360°',
        host='0.0.0.0',
        port=int(os.getenv('PORT', '8080')),
        reload=False,
        show=False
    )
