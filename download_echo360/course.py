# Copyright (c) Subramanya N. Licensed under the Apache License 2.0. All Rights Reserved
import logging

import json
import sys
import operator
import re
import dateutil.parser
import os

import selenium
import requests
import ffmpy

from selenium.common.exceptions import StaleElementReferenceException

from download_echo360.naive_m3u8_parser import NaiveM3U8Parser
from download_echo360.hls_downloader import Downloader

logging.basicConfig(
    format="[%(levelname)s: %(name)-12s] %(message)s",
    level=logging.ERROR)
_logger = logging.getLogger(__name__)

class Echo360Course(object):
    def __init__(self, uuid, hostname=None):
        super(Echo360Course, self).__init__()
        self._course_id = None
        self._course_name = None
        self._uuid = uuid
        self._videos = None
        self._driver = None
        if hostname is None:
            self._hostname = "https://login.echo360.org/login"
        else:
            self._hostname = hostname

    @property
    def uuid(self):
        return self._uuid

    @property
    def hostname(self):
        return self._hostname
    
    @property
    def url(self):
        return "{}/ess/portal/section/{}".format(self._hostname, self._uuid)

    @property
    def video_url(self):
        return "{}/section/{}/syllabus".format(self._hostname, self._uuid)

    @property
    def nice_name(self):
        return self.course_name

    @property
    def course_id(self):
        if self._course_id is None:
            self._course_id = ""
        return self._course_id

    @property
    def course_name(self):
        if self._course_name is None:
            # try each available video as some video might be special has contains
            # no information about the course.
            for v in self.course_data["data"]:
                try:
                    self._course_name = v["lesson"]["video"]["published"]["courseName"]
                    break
                except KeyError:
                    pass
            if self._course_name is None:
                # no available course name found...?
                self._course_name = "[[UNTITLED]]"
        return self._course_name
    
    def set_driver(self, driver):
        self._driver = driver
    
    def get_videos(self):
        assert self._driver is not None, "Driver not initialized"
        if self._videos is None:
            try:
                course_data_json = self._get_course_data()
                self._videos = Echo360Videos(videos_json=course_data_json["data"], driver=self._driver, hostname=self._hostname)
            except selenium.common.exceptions.NoSuchElementException as e:
                print("selenium cannot find given elements")
                raise e
            
        return self._videos

    def _get_course_data(self):
        try:
            self._driver.get(self.video_url)
            # use requests to retrieve data
            session = requests.Session()
            # load cookies from selenium
            for cookie in self._driver.get_cookies():
                session.cookies.set(cookie["name"], cookie["value"])
            
            request = session.get(self.video_url)
            if not request.ok:
                raise Exception("Error: Failed to get m3u8 info for EchoCourse!")
            
            json_str = request.text
        except ValueError as e:
            raise Exception("Unable to retrieve JSON (course_data) from url", e)
        self.course_data = json.loads(json_str)
        return self.course_data

def update_course_retrieval_progress(current, total):
    prefix = "> Retrieving couser information..."
    status = "{}/{} videos".format(current, total)
    text = "\r{0} {1} ".format(prefix, status)
    sys.stdout.write(text)
    sys.stdout.flush()
            
class Echo360Videos(object):
    def __init__(self, videos_json, driver, hostname, skip_video_on_error=True):
        super(Echo360Videos, self).__init__()
        assert videos_json is not None
        self._driver = driver
        self._videos = []
        total_videos = len(videos_json)
        update_course_retrieval_progress(0, total_videos)

        for i, video_json in enumerate(videos_json):
            try:
                self._videos.append(
                    Echo360Video(video_json=video_json, driver=driver, hostname=hostname)
                )
            except Exception:
                if not skip_video_on_error:
                    raise
            update_course_retrieval_progress(i + 1, total_videos)
        
        self._videos.sort(key=operator.attrgetter("date"))

    @property
    def videos(self):
        return self._videos

class Echo360Video(object):
    def __init__(self, video_json, driver, hostname):
        super(Echo360Video, self).__init__()
        self.hostname = hostname
        self._driver = driver
        self.video_json = video_json
        self.is_multipart_video = False
        self.sub_videos = [self]
        
        self._video_id = "{0}".format(video_json["lesson"]["lesson"]["id"])
        self._driver.get(self.video_url)
        _logger.info("Retrieving video information for {}".format(self.video_url))

        self._url = self.loop_find_m3u8_url(self.video_url, waitsecounds=30)
        self._date = self.get_date(video_json)
        self._title = video_json["lesson"]["lesson"]["name"]
    
    @property
    def video_url(self):
        return "{}/lesson/{}/classroom".format(self.hostname, self._video_id)
    
    @property
    def title(self):
        if type(self._title) != str:
            # it's type unicode for python2
            return self._title.encode("utf-8")
        return self._title
    
    @property
    def date(self):
        return self._date
    
    @property
    def url(self):
        return self._url
    
    def get_all_parts(self):
        return self.sub_videos

    def get_date(self, video_json):
        try:
            # date is not important so we will just ignore it if something went wrong
            # Also, some echoCloud videos returns None for video start time... :(
            date = dateutil.parser.parse(self._extract_date(video_json)).date()
            return date.strftime("%Y-%m-%d")
        except Exception:
            return "1970-01-01"
    
    def _extract_date(self, video_json):
        if self.is_multipart_video:
            if video_json["groupInfo"]["createdAt"] is not None:
                return video_json["groupInfo"]["createdAt"]
            if video_json["groupInfo"]["u'updatedAt'"] is not None:
                return video_json["groupInfo"]["u'updatedAt'"]

        if "startTimeUTC" in video_json["lesson"]:
            if video_json["lesson"]["startTimeUTC"] is not None:
                return video_json["lesson"]["startTimeUTC"]
        if "createdAt" in video_json["lesson"]["lesson"]:
            return video_json["lesson"]["lesson"]["createdAt"]

    def loop_find_m3u8_url(self, video_url, waitsecounds=15, max_attempts=5):
        def brute_force_get_url(suffix):
            # this is the first method I tried, which sort of works
            stale_attempt = 1
            refresh_attempt = 1
            while True:
                self._driver.get(video_url)
                try:
                    # the replace is for reversing the escape by the escapped js in the page source
                    urls = set(
                        re.findall(
                            'https://[^,"]*?[.]{}'.format(suffix),
                            self._driver.page_source.replace("\/", "/"),
                        )
                    )
                    return urls

                except selenium.common.exceptions.TimeoutException:
                    if refresh_attempt >= max_attempts:
                        print(
                            "\r\nERROR: Connection timeouted after {} second for {} attempts... \
                              Possibly internet problem?".format(
                                waitsecounds, max_attempts
                            )
                        )
                        raise
                    refresh_attempt += 1
                except StaleElementReferenceException:
                    if stale_attempt >= max_attempts:
                        print(
                            "\r\nERROR: Elements are not stable to retrieve after {} attempts... \
                            Possibly internet problem?".format(
                                max_attempts
                            )
                        )
                        raise
                    stale_attempt += 1

        def brute_force_get_mp4_url():
            urls = brute_force_get_url(suffix="mp4")
            if len(urls) == 0:
                raise Exception("None were found.")
            return sorted(urls)[:2]
        
        def from_json_m3u8():
            # seems like json would also contain that information so this method tries
            # to retrieve based on that
            if (not self.video_json["lesson"]["hasVideo"]
                or not self.video_json["lesson"]["hasAvailableVideo"]):
                return False
            manifests = self.video_json["lesson"]["video"]["media"]["media"]["versions"][0]["manifests"]
            m3u8urls = [m["uri"] for m in manifests]
            # somehow the hostname for these urls are from amazon (probably offloading
            # to them.) We need to set the host back to echo360.org
            from urllib.parse import urlparse
            new_m3u8urls = []
            new_hostname = urlparse(self.hostname).netloc
            for url in m3u8urls:
                parse_result = urlparse(url)
                new_m3u8urls.append(
                    "{}://content.{}{}".format(
                        parse_result.scheme, new_hostname, parse_result.path
                    )
                )
            return new_m3u8urls

        def from_json_mp4():
            mp4_files = self.video_json["lesson"]["video"]["media"]["media"]["current"][
                "primaryFiles"
            ]
            urls = [obj["s3Url"] for obj in mp4_files]
            if len(urls) == 0:
                raise ValueError("Cannot find mp4 urls")
            # usually hd is the last one. so we will sort in reverse order
            return next(reversed(urls))

        # try different methods in series, first the preferred ones, then the more
        # obscure ones.
        try:
            _logger.debug("Trying from_json_mp4 method")
            return from_json_mp4()
        except Exception as e:
            _logger.debug("Encountered exception: {}".format(e))
        try:
            _logger.debug("Trying from_json_m3u8 method")
            m3u8urls = from_json_m3u8()
        except Exception as e:
            _logger.debug("Encountered exception: {}".format(e))
        try:
            _logger.debug("Trying brute_force_all_mp4 method")
            return brute_force_get_mp4_url()
        except Exception as e:
            _logger.debug("Encountered exception: {}".format(e))
        try:
            _logger.debug("Trying brute_force_all_m3u8 method")
            m3u8urls = brute_force_get_url(suffix="m3u8")
        except Exception as e:
            _logger.debug("Encountered exception: {}".format(e))
            _logger.debug("All methods had been exhausted.")
            print("Tried all methods to retrieve videos but all had failed!")
            raise

        # find one that has audio + video
        m3u8urls = [url for url in m3u8urls if url.endswith("av.m3u8")]
        
        if len(m3u8urls) == 0:
            print(
                "No audio+video m3u8 files found! Skipping...\n"
                "This can either be \n(i) Credential failure? \n(ii) Logic error "
                "in the script. \n(iii) This lecture only provides audio?\n"
                "This script is hard-coded to download audio+video. \n"
                "If this is your intended behaviour, please contact the author."
            )
            return False
        
        m3u8urls = list(reversed(m3u8urls))
        
        return m3u8urls[:2]
    
    def download(self, output_dir, filename, pool_size=50):
        print("-" * 80)
        print("Downloading video: {}".format(filename))
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        session = requests.Session()
        for cookie in self._driver.get_cookies():
            session.cookies.set(cookie["name"], cookie["value"])
        
        urls = self.url
        if not isinstance(urls, list):
            urls = [urls]

        final_result = True
        for counter, single_url in enumerate(urls):
            print("- Downloading video feed {}...".format(counter + 1))
            new_filename = (
                (filename + str(counter + 1))
            )
            result = self.download_single(
                session, single_url, output_dir, new_filename, pool_size
            )
            final_result = final_result and result
        
        return final_result

    def download_single(self, session, single_url, output_dir, filename, pool_size=50):
        if single_url.endswith(".m3u8"):
            request = session.get(single_url)
            if not request.ok:
                print("ERROR: Cannot retrieve m3u8 file")
                return False
            
            lines = [n for n in request.content.decode().split("\n")]
            m3u8_video = None
            m3u8_audio = None

            m3u8_parser = NaiveM3U8Parser(lines)
            try:
                m3u8_parser.parse()
            except Exception as e:
                _logger.debug("Exception occurred while parsing m3u8: {}".format(e))
                print("Failed to parse m3u8. Skipping...")
                return False

            m3u8_video, m3u8_audio = m3u8_parser.get_video_and_audio()

            if m3u8_video is None:
                print("ERROR: Failed to find video m3u8... skipping this one")
                return False
            
            from download_echo360.hls_downloader import urljoin
            audio_file = None
            if m3u8_audio is not None:
                print("  > Downloading audio:")
                audio_file = self._download_url_to_dir(
                    urljoin(single_url, m3u8_audio),
                    output_dir,
                    filename + "_audio",
                    pool_size,
                    convert_to_mp4=False,
                )
            print("  > Downloading video:")
            video_file = self._download_url_to_dir(
                urljoin(single_url, m3u8_video),
                output_dir,
                filename + "_video",
                pool_size,
                convert_to_mp4=False,
            )
            sys.stdout.write("  > Converting to mp4... ")
            sys.stdout.flush()

            # combine audio file with video (separate audio might not exists.)
            if self.combine_audio_video(audio_file=audio_file,
                video_file=video_file, final_file=os.path.join(output_dir, filename + ".mp4")):
                # remove left-over plain audio/video files. (if mixing was successful)
                if audio_file is not None:
                    os.remove(audio_file)
                os.remove(video_file)
        else: 
            import tqdm

            r = session.get(single_url, stream=True)
            total_size = int(r.headers.get("content-length", 0))
            block_size = 1024  # 1 kilobyte
            with tqdm.tqdm(total=total_size, unit="iB", unit_scale=True) as pbar:
                with open(os.path.join(output_dir, filename + ".mp4"), "wb") as f:
                    for data in r.iter_content(block_size):
                        pbar.update(len(data))
                        f.write(data)

        print("Done!")
        print("-" * 60)
        return True
    
    def _download_url_to_dir(
        self, url, output_dir, filename, pool_size, convert_to_mp4=True):
        echo360_downloader = Downloader(
            pool_size, selenium_cookies=self._driver.get_cookies()
        )
        echo360_downloader.run(url, output_dir, convert_to_mp4=convert_to_mp4)

        # rename file
        ext = echo360_downloader.result_file_name.split(".")[-1]
        result_full_path = os.path.join(output_dir, "{0}.{1}".format(filename, ext))
        os.rename(os.path.join(echo360_downloader.result_file_name), result_full_path)
        return result_full_path

    @staticmethod
    def combine_audio_video(audio_file, video_file, final_file):
        if os.path.exists(final_file):
            os.remove(final_file)
        _inputs = {}
        _inputs[video_file] = None
        if audio_file is not None:
            _inputs[audio_file] = None
        try:
            ff = ffmpy.FFmpeg(
                global_options="-loglevel panic",
                inputs=_inputs,
                outputs={final_file: ["-c:v", "copy", "-c:a", "ac3"]},
            )
            ff.run()
        except ffmpy.FFExecutableNotFoundError:
            print(
                '[WARN] Skipping mixing of audio/video because "ffmpeg" not installed.'
            )
            return False
        except ffmpy.FFRuntimeError:
            print(
                "[Error] Skipping mixing of audio/video because ffmpeg exited with non-zero status code."
            )
            return False
        return True