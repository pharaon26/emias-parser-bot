import asyncio
import logging
import os
import re
import time
import random

from dotenv import load_dotenv

load_dotenv()

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from emias_checker import (
    LoggedOutError,
    diff_availability,
    get_availability,
    make_driver,
    open_new_appointment,
    scrape_tabs,
    click_and_scrape_next,
)
from state_store import load_state, save_state
from targets_store import add_target, load_targets, remove_target

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OWNER_CHAT_ID = int(os.environ['OWNER_CHAT_ID'])
CHECK_INTERVAL_MINUTES = int(os.environ.get('CHECK_INTERVAL_MINUTES', 10))
LOGGED_OUT_COOLDOWN_SECONDS = 3600

PERSON, TAB_CB, OPTION_CB, CLINICS_CB = range(4)

ADD_BUTTON = '➕ Добавить'
LIST_BUTTON = '📋 Мои записи'
CANCEL_BUTTON = '❌ Отмена'


def _with_cancel(keyboard_rows):
    return keyboard_rows + [[InlineKeyboardButton(CANCEL_BUTTON, callback_data='cancel_add')]]


MAIN_KEYBOARD = ReplyKeyboardMarkup([[ADD_BUTTON, LIST_BUTTON]], resize_keyboard=True)


def owner_only(handler):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != OWNER_CHAT_ID:
            return
        return await handler(update, context)

    return wrapped


async def run_blocking(func, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func, *args)


async def get_driver(context):
    if context.bot_data.get('driver') is None:
        context.bot_data['driver'] = await run_blocking(make_driver)
    return context.bot_data['driver']


def get_driver_lock(context):
    if 'driver_lock' not in context.bot_data:
        context.bot_data['driver_lock'] = asyncio.Lock()
    return context.bot_data['driver_lock']


def get_person_buttons():
    buttons = ['Я']
    seen_lower = {'я'}

    for t in load_targets():
        name = (t.get('person') or '').strip()
        if name and name.lower() not in seen_lower:
            seen_lower.add(name.lower())
            buttons.append(name)

    return buttons


@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        'Слежу за свободными окошками в ЕМИАС — показываю кнопками, что '
        'реально есть на сайте сейчас, вводить текст руками почти не нужно.',
        reply_markup=MAIN_KEYBOARD,
    )


@owner_only
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = get_person_buttons()
    keyboard = [[InlineKeyboardButton(p, callback_data=f'person:{p}')] for p in buttons]
    await update.message.reply_text(
        'Для кого это? Можно нажать кнопку или просто написать имя:',
        reply_markup=InlineKeyboardMarkup(_with_cancel(keyboard)),
    )
    return PERSON


async def person_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.message.chat.id != OWNER_CHAT_ID:
        return
    await query.answer()

    person = query.data.split(':', 1)[1]
    context.user_data['person'] = person
    await query.edit_message_text(f'Для кого: {person}')
    return await _start_tab_scan(query.message, context)


async def add_person(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['person'] = update.message.text.strip().capitalize()
    return await _start_tab_scan(update.message, context)


async def _start_tab_scan(message, context):
    await message.reply_text('Секунду, открываю мастер записи на сайте…')

    driver = await get_driver(context)
    lock = get_driver_lock(context)

    try:
        async with lock:
            await run_blocking(open_new_appointment, driver)
            tabs = await run_blocking(scrape_tabs, driver)
    except LoggedOutError:
        await message.reply_text(
            'Сессия ЕМИАС слетела. Зайди в Chrome с рабочим профилем и '
            'залогинься заново, потом попробуй ещё раз.'
        )
        context.user_data.clear()
        return ConversationHandler.END

    if not tabs:
        await message.reply_text('Не нашёл вкладок на сайте, странно. Попробуй ещё раз.')
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data['path'] = []
    context.user_data['tabs'] = tabs
    keyboard = [[InlineKeyboardButton(t, callback_data=f'tab:{i}')] for i, t in enumerate(tabs)]
    await message.reply_text('Как будем записываться?', reply_markup=InlineKeyboardMarkup(_with_cancel(keyboard)))
    return TAB_CB


async def tab_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.message.chat.id != OWNER_CHAT_ID:
        return
    await query.answer()

    idx = int(query.data.split(':')[1])
    tab_text = context.user_data['tabs'][idx]
    context.user_data['path'].append(tab_text)

    await query.edit_message_text(f'Выбрано: {tab_text}\nСмотрю доступные варианты…')

    driver = await get_driver(context)
    lock = get_driver_lock(context)

    async with lock:
        screen_type, data = await run_blocking(click_and_scrape_next, driver, tab_text)

    return await _handle_screen_type(query.message, context, screen_type, data)


async def option_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.message.chat.id != OWNER_CHAT_ID:
        return
    await query.answer()

    idx = int(query.data.split(':')[1])
    option_text = context.user_data['current_options'][idx]
    context.user_data['path'].append(option_text)

    await query.edit_message_text(f'Выбрано: {option_text}\nСмотрю дальше…')

    driver = await get_driver(context)
    lock = get_driver_lock(context)

    async with lock:
        screen_type, data = await run_blocking(click_and_scrape_next, driver, option_text)

    return await _handle_screen_type(query.message, context, screen_type, data)


async def _handle_screen_type(message, context, screen_type, data):
    if screen_type == 'options':
        if not data:
            await message.reply_text('Здесь пока пусто. Попробуй начать заново.')
            context.user_data.clear()
            return ConversationHandler.END

        context.user_data['current_options'] = data
        keyboard = [[InlineKeyboardButton(o, callback_data=f'opt:{i}')] for i, o in enumerate(data)]
        await message.reply_text('Выбирай:', reply_markup=InlineKeyboardMarkup(_with_cancel(keyboard)))
        return OPTION_CB

    elif screen_type == 'clinics':
        clinics_map = data
        context.user_data['clinics_options'] = list(clinics_map.items())
        context.user_data['selected_clinics'] = set()

        if not clinics_map:
            await message.reply_text(
                'Сейчас по этому пути нет доступных врачей ни в одном '
                'филиале. Сохраню без фильтра — буду проверять все филиалы.'
            )
            return await _finish_add(message, context)

        await message.reply_text(
            'В каких филиалах интересно? Отметь нужные и нажми «Готово» '
            '(если ничего не отметить — буду проверять все).',
            reply_markup=_build_clinics_keyboard(context),
        )
        return CLINICS_CB


def _build_clinics_keyboard(context):
    options = context.user_data['clinics_options']
    selected = context.user_data['selected_clinics']
    keyboard = []
    for i, (name, address) in enumerate(options):
        mark = '✅ ' if name in selected else ''
        keyboard.append([InlineKeyboardButton(f'{mark}{name}', callback_data=f'clinic:{i}')])
    keyboard.append([InlineKeyboardButton('Готово', callback_data='clinics_done')])
    return InlineKeyboardMarkup(_with_cancel(keyboard))


async def clinic_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.message.chat.id != OWNER_CHAT_ID:
        return
    await query.answer()

    idx = int(query.data.split(':')[1])
    name, _address = context.user_data['clinics_options'][idx]
    selected = context.user_data['selected_clinics']

    if name in selected:
        selected.discard(name)
    else:
        selected.add(name)

    await query.edit_message_reply_markup(reply_markup=_build_clinics_keyboard(context))
    return CLINICS_CB


async def clinics_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.message.chat.id != OWNER_CHAT_ID:
        return
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    return await _finish_add(query.message, context)


async def _finish_add(message, context):
    data = context.user_data
    path = data['path']
    label = f"{path[-1]} ({data['person']})"
    target = {
        'label': label,
        'person': data['person'],
        'path': path,
        'clinics': sorted(data.get('selected_clinics', [])),
    }
    add_target(target)

    await message.reply_text(
        f'Добавил: {label}. Буду проверять и уведомлять о свободных окошках.',
        reply_markup=MAIN_KEYBOARD,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.message.chat.id != OWNER_CHAT_ID:
        return
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text('Отменил добавление.')
    await query.message.reply_text('Готов к следующему разу.', reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


@owner_only
async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text('Отменил добавление.', reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


@owner_only
async def list_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    targets = load_targets()
    if not targets:
        await update.message.reply_text('Пока ничего не отслеживается. Нажми «Добавить».', reply_markup=MAIN_KEYBOARD)
        return

    for i, t in enumerate(targets):
        clinics = ', '.join(t['clinics']) if t.get('clinics') else 'любой филиал'
        text = f"{t['label']}\nФилиал: {clinics}"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton('Удалить', callback_data=f'remove:{i}')]]
        )
        await update.message.reply_text(text, reply_markup=keyboard)


async def remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.message.chat.id != OWNER_CHAT_ID:
        return
    await query.answer()

    index = int(query.data.split(':')[1])
    removed = remove_target(index)

    if removed:
        await query.edit_message_text(f"Удалил: {removed['label']}")
    else:
        await query.edit_message_text('Уже удалено.')


def _format_notification(label, new_entries, changed_entries):
    lines = [f'{label}:']
    for item in new_entries:
        lines.append(
            f"Новый вариант — {item['doctor']} ({item['clinic']}): {item['earliest_date']}"
        )
    for item, old_date in changed_entries:
        lines.append(
            f"Дата изменилась — {item['doctor']} ({item['clinic']}): "
            f"было «{old_date}», стало «{item['earliest_date']}»"
        )
    return '\n'.join(lines)


async def run_checks(context: ContextTypes.DEFAULT_TYPE):
    try:
        targets = load_targets()
        if not targets:
            return

        state = load_state()
        driver = await get_driver(context)
        lock = get_driver_lock(context)

        for target in targets:
            label = target['label']
            prev_snapshot = state['seen'].get(label, {})

            try:
                async with lock:
                    listing = await run_blocking(get_availability, driver, target)
            except LoggedOutError:
                now = time.time()
                if now - state.get('logged_out_notified_at', 0) > LOGGED_OUT_COOLDOWN_SECONDS:
                    await context.bot.send_message(
                        OWNER_CHAT_ID,
                        'Сессия ЕМИАС слетела. Зайди в Chrome с рабочим профилем '
                        f"({os.environ['CHROME_PROFILE_DIR']}) и залогинься заново.",
                    )
                    state['logged_out_notified_at'] = now
                break
            except Exception as e:
                logger.warning(f"Ошибка при проверке '{label}': {e}")
                continue

            new_entries, changed_entries, new_snapshot = diff_availability(prev_snapshot, listing)

            if new_entries or changed_entries:
                text = _format_notification(label, new_entries, changed_entries)
                await context.bot.send_message(OWNER_CHAT_ID, text)
            else:
                logger.info(f'{label}: без изменений ({len(listing)} вариантов всего)')

            state['seen'][label] = new_snapshot

        save_state(state)

    finally:
        # Этот блок отработает ВСЕГДА, даже если targets пустой или была ошибка в коде выше.
        # Генерируем случайную добавку: от 1 до 7 минут (в секундах)
        jitter_seconds = random.randint(60, 7 * 60)
        next_run_in = (CHECK_INTERVAL_MINUTES * 60) + jitter_seconds

        logger.info(f"Следующая проверка запланирована через {next_run_in // 60} мин {next_run_in % 60} сек.")

        # Планируем следующий запуск
        context.job_queue.run_once(run_checks, next_run_in)


async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand('add', 'Добавить новую специальность для отслеживания'),
        BotCommand('list', 'Показать, что сейчас отслеживается'),
        BotCommand('cancel', 'Отменить текущий диалог добавления'),
    ])


def main():
    token = os.environ['TELEGRAM_BOT_TOKEN']
    application = Application.builder().token(token).post_init(post_init).build()

    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler('add', add_start),
            MessageHandler(filters.Regex(f'^{re.escape(ADD_BUTTON)}$'), add_start),
        ],
        states={
            PERSON: [
                CallbackQueryHandler(cancel_callback, pattern=r'^cancel_add$'),
                CallbackQueryHandler(person_callback, pattern=r'^person:'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_person),
            ],
            TAB_CB: [
                CallbackQueryHandler(cancel_callback, pattern=r'^cancel_add$'),
                CallbackQueryHandler(tab_callback, pattern=r'^tab:'),
            ],
            OPTION_CB: [
                CallbackQueryHandler(cancel_callback, pattern=r'^cancel_add$'),
                CallbackQueryHandler(option_callback, pattern=r'^opt:'),
            ],
            CLINICS_CB: [
                CallbackQueryHandler(cancel_callback, pattern=r'^cancel_add$'),
                CallbackQueryHandler(clinic_toggle_callback, pattern=r'^clinic:'),
                CallbackQueryHandler(clinics_done_callback, pattern=r'^clinics_done$'),
            ],
        },
        fallbacks=[CommandHandler('cancel', add_cancel)],
    )

    application.add_handler(CommandHandler('start', start))
    application.add_handler(add_conv)
    application.add_handler(CommandHandler('list', list_targets))
    application.add_handler(MessageHandler(filters.Regex(f'^{re.escape(LIST_BUTTON)}$'), list_targets))
    application.add_handler(CallbackQueryHandler(remove_callback, pattern=r'^remove:'))

    # Запускаем первую проверку через 10 секунд (run_once вместо run_repeating),
    # а дальше она сама себя будет зацикливать через try...finally
    application.job_queue.run_once(run_checks, 10)

    application.run_polling()


if __name__ == '__main__':
    main()