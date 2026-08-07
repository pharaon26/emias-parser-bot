import logging
import os
import time

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

logger = logging.getLogger(__name__)

CHROME_PROFILE_DIR = os.path.expanduser(os.environ['CHROME_PROFILE_DIR'])
CREATE_URL = 'https://emias.info/app/einfo/#/create'
LOGIN_URL_MARKER = 'login.mos.ru'

CLINIC_BLOCK_XPATH = "//div[contains(@class, 'theme-border') and .//h1]"


class LoggedOutError(Exception):
    pass


def make_driver():
    options = Options()
    options.add_argument(f'--user-data-dir={CHROME_PROFILE_DIR}')
    options.add_argument('--disable-blink-features=AutomationControlled')
    return webdriver.Chrome(options=options)


def _text(el):
    """Текст элемента через textContent, а не Selenium .text."""
    return (el.get_attribute('textContent') or '').strip()


def _click_by_text(driver, text, tag='span'):
    """Ищет элемент, чей текст похож на переданный — без учёта регистра."""
    target = ' '.join(text.strip().lower().split())

    elements = WebDriverWait(driver, 10).until(
        EC.presence_of_all_elements_located((By.XPATH, f'//{tag}'))
    )

    for el in elements:
        el_text = ' '.join(el.text.strip().lower().split())
        if not el_text:
            continue
        if target in el_text or el_text in target:
            try:
                el.click()
                return
            except Exception:
                continue

    raise NoSuchElementException(
        f"Не нашёл на странице ничего похожего на «{text}» (тег {tag})"
    )


def _scrape_specialty_listing(driver):
    """Сканирует текущую страницу специальности: какие клиники и врачи
    сейчас реально показаны."""
    results = []

    logger.info(f'Сканирую страницу: {driver.current_url}')
    logger.info(f'Заголовок страницы (page title тега): {driver.title!r}')

    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, CLINIC_BLOCK_XPATH))
        )
    except TimeoutException:
        all_h1 = driver.find_elements(By.TAG_NAME, 'h1')
        theme_border_divs = driver.find_elements(
            By.XPATH, "//div[contains(@class, 'theme-border')]"
        )
        logger.warning(
            f'CLINIC_BLOCK_XPATH не дождался за 15 сек. '
            f'Всего <h1> на странице: {len(all_h1)} '
            f'(тексты: {[h.text.strip() for h in all_h1]}). '
            f"Всего div с классом 'theme-border': {len(theme_border_divs)}."
        )
        return results

    clinic_blocks = driver.find_elements(By.XPATH, CLINIC_BLOCK_XPATH)
    logger.info(f'Найдено clinic_blocks: {len(clinic_blocks)}')

    for block in clinic_blocks:
        try:
            clinic_name = _text(block.find_element(By.TAG_NAME, 'h1'))
        except NoSuchElementException:
            continue

        try:
            clinic_address = _text(block.find_element(By.XPATH, './/a//span'))
        except NoSuchElementException:
            clinic_address = ''

        doctor_buttons = block.find_elements(By.XPATH, ".//li//button")
        logger.info(f"Клиника «{clinic_name}» ({clinic_address}): найдено кнопок врачей — {len(doctor_buttons)}")

        for button in doctor_buttons:
            spans = button.find_elements(By.TAG_NAME, 'span')
            span_texts = [_text(s) for s in spans]
            logger.info(f'  Кнопка: {len(spans)} span-ов, тексты: {span_texts!r}')

            if len(spans) < 2:
                continue

            doctor_name = _text(spans[0])
            earliest_date = _text(spans[-1])
            if doctor_name and earliest_date:
                results.append({
                    'clinic': clinic_name,
                    'clinic_address': clinic_address,
                    'doctor': doctor_name,
                    'earliest_date': earliest_date,
                })
            else:
                logger.info(
                    f'  Пропущено: doctor_name={doctor_name!r}, earliest_date={earliest_date!r}'
                )

    return results


def _clinic_matches(item, wanted_clinics):
    """Сравнивает клинику найденного врача с тем, что указал пользователь."""
    if not wanted_clinics:
        return True

    haystack = f"{item['clinic']} {item.get('clinic_address', '')}".lower()

    for wanted in wanted_clinics:
        wanted_norm = wanted.strip().lower()
        if wanted_norm and wanted_norm in haystack:
            return True

    return False


def click_exact_text(driver, text, tag='span'):
    """Кликает по элементу с точным совпадением текста."""
    normalized = ' '.join(text.strip().split())

    elements = WebDriverWait(driver, 10).until(
        EC.presence_of_all_elements_located((By.XPATH, f'//{tag}'))
    )

    for el in elements:
        el_text = ' '.join(_text(el).split())
        if el_text == normalized:
            el.click()
            return

    raise NoSuchElementException(
        f"Не нашёл элемент с текстом ровно «{text}» (тег {tag})"
    )


def scrape_tabs(driver):
    """Собирает варианты верхних вкладок (Цель, Направление, Специальность)."""
    elements = WebDriverWait(driver, 10).until(
        EC.presence_of_all_elements_located((By.XPATH, "//button[contains(@class, 'border-b-2')]//span"))
    )
    tabs = []
    for el in elements:
        text = _text(el)
        if text and text not in tabs:
            tabs.append(text)
    return tabs


def scrape_option_list(driver):
    """Собирает варианты с текущего экрана мастера записи (сетка кнопок)."""
    spans = driver.find_elements(
        By.XPATH,
        "//ul[contains(@class,'grid')]/li/button//span[contains(@class,'font-cera')]",
    )
    options = []
    for span in spans:
        text = _text(span)
        if text and text not in options:
            options.append(text)
    return options


def click_and_scrape_next(driver, text):
    """Универсальный шагатель: кликает по тексту и решает, что на новом экране
    (новые опции для выбора или уже список доступных поликлиник)."""
    click_exact_text(driver, text)
    time.sleep(1.5)

    options = scrape_option_list(driver)
    if options:
        return 'options', options

    clinics = scrape_clinics_for_current_specialty(driver)
    return 'clinics', clinics


def open_new_appointment(driver):
    """Открывает мастер записи с чистого листа."""
    driver.get(CREATE_URL)
    driver.refresh()
    time.sleep(2)

    if LOGIN_URL_MARKER in driver.current_url:
        raise LoggedOutError('Сессия ЕМИАС слетела — редирект на страницу входа mos.ru')

    _click_by_text(driver, 'Новая запись')
    time.sleep(1)


def scrape_clinics_for_current_specialty(driver):
    """Возвращает {короткое название клиники: адрес} для текущего экрана."""
    listing = _scrape_specialty_listing(driver)
    clinics = {}
    for item in listing:
        clinics[item['clinic']] = item.get('clinic_address', '')
    return clinics


def get_availability(driver, target):
    open_new_appointment(driver)

    if 'path' in target:
        for step_text in target['path']:
            click_exact_text(driver, step_text)
            time.sleep(1.5)
    else:
        # Обратная совместимость для записей, добавленных до апдейта
        click_exact_text(driver, target['goal_text'])
        time.sleep(1)
        click_exact_text(driver, target['specialty_text'])
        time.sleep(2)

    listing = _scrape_specialty_listing(driver)

    clinics = target.get('clinics') or []
    listing = [item for item in listing if _clinic_matches(item, clinics)]

    return listing


def diff_availability(previous_snapshot, current_listing):
    """Сравнивает прошлый результат с текущим."""
    new_snapshot = {}
    new_entries = []
    changed_entries = []

    for item in current_listing:
        key = f"{item['clinic']}||{item['doctor']}"
        new_snapshot[key] = item['earliest_date']

        if key not in previous_snapshot:
            new_entries.append(item)
        elif previous_snapshot[key] != item['earliest_date']:
            changed_entries.append((item, previous_snapshot[key]))

    return new_entries, changed_entries, new_snapshot