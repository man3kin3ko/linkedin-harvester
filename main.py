import re
import json
import typer
import pickle
import requests
from os import remove
from bs4 import BeautifulSoup as bs
from typing import List, Optional
from typing_extensions import Annotated
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.text import Text
from rich import print as rprint
from itertools import product


class Transcriptor:
    def __init__(self):
        self.name_patterns = {
            "en_name": re.compile("^[a-zA-Z]{2,} [a-zA-Z]{2,}$"),
            "ru_name": re.compile("^[а-яА-Я]{2,} [а-яА-Я]{2,}$"),
            "triple_name": re.compile(
                "^[a-zA-Zа-яА-Я]{2,} [a-zA-Zа-яА-Я]*.? [a-zA-Zа-яА-Я]{2,}$"
            ),
        }

        self.cyrillic_patterns = json.loads(
            open("patterns.json", encoding="utf-8").read()
        )

    def transcript(self, name: str):
        if re.match(self.name_patterns["en_name"], name):
            return [name.lower()]  # also add en_to_en cyrillic patterns
        elif re.match(self.name_patterns["ru_name"], name):
            return self.cyrillic_comb(name.lower())
        elif re.match(self.name_patterns["triple_name"], name):
            raise NotImplementedError("Triple names are not supported")
        else:
            raise ValueError("Can not find suitable regular expression")

    def cyrillic_comb(self, name: str):
        perms: list[list] = []  # list of patterns
        name_variations: list[str] = []  # result list

        for char in name:
            pattern = self.cyrillic_patterns[char]
            perms.append(pattern)

        for prod in product(*perms):
            name_variations.append("".join(prod))

        return name_variations


class EmailBuilder:
    def __init__(self, domains: list, full_name=True):
        self.full_name = full_name
        self.domains = self.clear_domains(domains)

    def clear_domains(self, domains: list):
        email_pattern = re.compile("^(@?)([a-z]+\.[a-z]{2,10})$")
        return [re.match(email_pattern, i).group(2) for i in domains]

    def convert_name(self, name: str):
        # Здесь изменять логику, чтобы билдить разные форматы (возможно нужно рефакторнуть)
        # Если зовут aleksandr panov
        # то могут варианты apanov@domain.com, alpanov@domain.com, alepanov@domain.com и тд

        fisrt_name, last_name = name.lower().split(" ")
        if self.full_name:
            return f"{fisrt_name}.{last_name}@"
        return f"{fisrt_name[0]}.{last_name}@"

    def build_emails(self, names: list):
        return map(
            lambda x: "".join(x),
            product([self.convert_name(i) for i in names], self.domains),
        )


class LinkedinParser:
    def __init__(self, li_at: str, csrf_token: str, company_url: str):
        self.cookies = {"li_at": li_at, "JSESSIONID": csrf_token}
        self.headers = {"Csrf-Token": csrf_token}
        self.company_id = self.parse_request_company(company_url)
        self.people_num = 1

    def make_people_req(self, start: int, company_id: int):
        resp = self.make_linedin_request(
            f"https://linkedin.com/voyager/api/graphql?variables=(start:{start},query:(flagshipSearchIntent:SEARCH_SRP,queryParameters:List((key:currentCompany,value:List({company_id})),(key:resultType,value:List(PEOPLE))),includeFiltersInResponse:false))&&queryId=voyagerSearchDashClusters.711fd1976049eeb7ac5496821697249f",
            use_post=True,
        )
        return resp.text

    def make_company_req(self, company_url):
        resp = self.make_linedin_request(company_url, use_get=True)
        return resp.text

    def parse_request_company(self, company_url):
        html = bs(self.make_company_req(company_url), "html.parser")
        code_block = html.find_all("code")[16]
        try:
            universal_name = json.loads(code_block.text)["data"]["data"]["organizationDashCompaniesByUniversalName"]["*elements"][0]
            id_ = int(universal_name.split(":")[-1])
            return id_
        except KeyError:
            raise Exception("Seems like LinkedIn changed HTML layout. It's time to debug, honey!")

    def make_linedin_request(self, url: str, use_get=False, use_post=True):
        if use_get:
            return requests.get(
                url,
                cookies=self.cookies,
                headers=self.headers,
            )
        if use_post:
            return requests.post(
                url,
                cookies=self.cookies,
                headers=self.headers,
            )

    def parse_request_people(self, start: int):
        names = []
        resp = json.loads(self.make_people_req(start, self.company_id))  # res.json()?
        if start == 0:
            self.people_num = resp["data"]["searchDashClustersByAll"]["metadata"][
                "totalResultCount"
            ]
        elements = resp["data"]["searchDashClustersByAll"]["elements"]
        for i in elements[int(start == 0)]["items"]:
            name = i["item"]["entityResult"]["title"]["text"]
            if name != "LinkedIn Member":
                names.append(name)
        return names

    def parse(self):
        start = 0
        names = []
        while self.people_num:
            names += self.parse_request_people(start)
            if (self.people_num // 10) > 0:
                start += 10
                self.people_num -= 10
            else:
                start += self.people_num
                self.people_num = 0
        return names


class Validator:
    def __init__(self, url: str):
        self.validator_url = url
        self.headers = {"Content-Type": "application/json"}

    def validate(self, email: str):
        # Здесь я хочу добавить возможноcть полиморфно менять модули
        # чтобы можно было валидировать почты об Azure и другие провайдеры
        # Наверное для этого нужно создать общий класс Validator и от него наследовать валидаторы-поменьше
        # c интерфейсом .validate()
        res = requests.post(
            self.validator_url,
            data=json.dumps({"to_email": email}),
            headers=self.headers,
        ).json()
        is_reachable = res.get("is_reachable")
        if is_reachable == None:
            raise Exception("Validator server sent undefined answer")
        if res.get("mx").get("accepts_mail") and not res.get("smtp").get("is_disabled"):
            return is_reachable
        return "invalid"


class Logic:
    def __init__(
        self,
        li_at: str,
        csrf_token: str,
        validator_url: str,
        domains: List[str],
        company_url: str,
        cli,
    ):
        self.trans = Transcriptor()
        self.parser = LinkedinParser(li_at, csrf_token, company_url)
        self.validator = Validator(validator_url)
        self.builder = EmailBuilder(domains)
        self.names = None
        self.cli = cli

    def harvest_linkedin(self):
        try:
            self.names = self.parser.parse()
        except requests.exceptions.TooManyRedirects:
            self.cli.print("Seems like your cookies are outdated")
            exit(1)
        except requests.exceptions.SSLError:
            self.cli.print("Did you use VPN?")
            exit(2)
        with open("linkedin_parsed_names.tmp", "wb") as tmp:
            pickle.dump(self.names, tmp)

    def build_emails(self):
        open("builded_emails.tmp", "w").close()
        for person in self.names:
            with open("builded_emails.tmp", "a") as f:
                try:
                    f.writelines(
                        map(
                            lambda x: f"{x}\n",
                            self.builder.build_emails(self.trans.transcript(person)),
                        )
                    )
                except NotImplementedError:
                    self.cli.print(f"{person} email is not builded", err=False)
                except ValueError:
                    self.cli.print(f"{person} did not suite any pattern")
        remove("linkedin_parsed_names.tmp")

    def validate_smtp(self):
        with open("builded_emails.tmp") as tmp, open("valid_emails", "w") as done:
            for line in tmp.readlines():
                email = line.rstrip("\n")
                is_valid = self.validator.validate(email)
                if is_valid != "invalid":
                    done.write(f"{email},{is_valid}\n")


app = typer.Typer()


@app.command()
class Cli:
    cli_strings = json.loads(open("cli_strings.json", encoding="utf-8").read())

    def __init__(
        self,
        li_at: Annotated[str, typer.Option(help=cli_strings["li_at"])],
        csrf_token: Annotated[str, typer.Option(help=cli_strings["csrf_token"])],
        validator_url: Annotated[str, typer.Option(help=cli_strings["validator_url"])],
        linkedin_organisation_page: Annotated[
            str, typer.Option(help=cli_strings["linkedin_url"])
        ],
        email_domains: List[str],
        resume: Annotated[Optional[str], typer.Option()] = None,
    ):
        self.logic = Logic(
            li_at,
            csrf_token,
            validator_url,
            email_domains,
            linkedin_organisation_page,
            self,
        )
        self.run()

    def print(self, string: str, err=True):
        text = Text()
        if err:
            text.append("\n[❌] ")
            style = "red"
        else:
            text.append("\n[📝] ")
            style = ""
        text.append(string, style=style)
        rprint(text)

    @staticmethod
    def show_banner():
        banner = """
          ⠀⠀⠀⠀⢤⣶⣄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⣤⡾⠿⢿⡀⠀⠀⠀⠀⣠⣶⣿⣿⣷⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⢀⣴⣦⣴⣿⡋⠀⠀⠈⢳⡄⠀⢠⣾⣿⠁  ⠈⣿⡆⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⣰⣿⣿⠿⠛⠉⠉⠁⠀⠀⠀⠹⡄⣿⣿´• ω•`⣿⠀⠀⠀
⠀⠀⠀⠀⠀⣠⣾⡿⠋⠁⠀⠀⠀⠀⠀⠀⠀⠀⣰⣏⢻⣿⡆ ⠀  ⠸⣿⠀⠀⠀
⠀⠀⠀⢀⣴⠟⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣾⣿⣿⣆⠹⣷ ⠀  ⢘⣿⠀⠀⠀
⠀⠀⢀⡾⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢰⣿⣿⠋⠉⠛⠂⠹⠿⣲⣿⣿⣿⣿⣧⠀⠀
⠀⢠⠏⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣤⣿⣿⣿⣷⣾⣿⡇⢀⠀⣼⣿⣿⣿⣿⣿⣧⠀
⠰⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⡘⢿⣿⣿⣿⣿⣿⠀
⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⣷⡈⠿⣿⣿⢿⣿⡆
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⠛⠁⢙⠛⣿⣿⣿⣿⡟⠀⡿⠀⠀⢀⣿⣿⣿⡇
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⣶⣤⣉⣛⠻⠇⢠⣿⣾⣿⡄⢻⣿⣿⡇
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣦⣤⣾⣿⣿⣿⣿⣆⠁⣿⡇

⠀⠀⠀⠀         🌾⠀LinkedIn Harvester 🌾⠀
"""
        rprint(banner)

    def run(self):
        self.add_progress_bar(self.logic.harvest_linkedin, "Parsing Linkedin")
        self.add_progress_bar(self.logic.build_emails, "Building emails")
        self.add_progress_bar(
            self.logic.validate_smtp, "Validating emails using SMTP protocol"
        )

    def add_progress_bar(self, function, description):
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
        ) as progress:
            progress.add_task(description=description, total=None)
            function()


if __name__ == "__main__":
    Cli.show_banner()
    app()
