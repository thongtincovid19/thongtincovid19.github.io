import datetime
import json
import urllib.request

import pandas as pd
import tabula

import localization


QUERY_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
}
FIREBASE_BATCH_SIZE = 499  # Max = 500


def batch_data(iterable, n=1):
    """Divide data into batches of fix length."""
    l = len(iterable)
    for ndx in range(0, l, n):
        yield iterable[ndx:min(ndx + n, l)]


class Dataset(object):
    def __init__(self, url, name, **kwargs):
        self.url = url
        self.name = name
        self.dataframe = None
        self.kwargs = kwargs

    def query_all(self):
        if self.dataframe is None:
            self.dataframe = self._create_dataframe()
            self._localize()
            self._cleanse()

        return self.dataframe

    def _create_dataframe(self):
        raise NotImplementedError()

    def _localize_column_names(self):
        col_list = [self.__class__.__dict__[x] for x in self.__class__.__dict__ if x.startswith('COL_')]
        self.dataframe.columns = col_list
        return self.dataframe

    def _localize_date(self, column, na_value='Đang điều tra', inplace=True):
        t = self.dataframe[column].str.extract(r'([0-9]+)月([0-9]+)日')
        series = t[0] + '/' + t[1]
        series.fillna(na_value, inplace=True)
        if inplace:
            self.dataframe[column] = series

        return series

    def _localize_age(self, column, na_value='Không rõ', inplace=True):
        series = self.dataframe[column].str.replace('代', 's')
        series.replace({
            '1歳未満': 'Dưới 1',
            '未就学児': 'Dưới 3',
            '就学児': '3-9',
            '10歳未': 'Dưới 10',
            '10歳未満': 'Dưới 10',
            '90s以上': 'Trên 90',
            '90歳以上': 'Trên 90',
            '100歳以': 'Trên 100',
            '100歳以上': 'Trên 100',
            '100s以上': 'Trên 100',
            '不': na_value,
            '－': na_value,
            'ー': na_value,
            '調査中': na_value,
            '非公表': na_value,
            '同意なし': na_value,
            '公表しない': na_value,
        }, inplace=True)
        series.fillna(na_value, inplace=True)
        if inplace:
            self.dataframe[column] = series

        return series

    def _localize_sex(self, column, na_value='Không công bố', inplace=True):
        series = self.dataframe[column].replace({
            '男性': 'Nam',
            '女性': 'Nữ',
            '女児': 'Nữ',
            '調査中': na_value,
            '－': na_value,
            '同意なし': na_value,
            '非公表': na_value,
            '公表しない': na_value,
            '不明': na_value,
        })
        series.fillna(na_value, inplace=True)
        if inplace:
            self.dataframe[column] = series

        return series

    def _localize_boolean(self, column, na_value=0, inplace=True):
        series = self.dataframe[column].replace({
            '〇': 1,
            '○': 1,
            '': na_value,
        })
        series.fillna(na_value, inplace=True)
        series = series.astype(int)
        if inplace:
            self.dataframe[column] = series

        return series

    def _localize_location(
        self,
        column,
        localization_dict,
        insider_keys,
        insider_value='Trong tỉnh',
        outsider_keys=None,
        outsider_value='Ngoài tỉnh',
        na_keys=None,
        na_value='Đang điều tra',
        others=None,
        inplace=True,
    ):
        if na_keys is None:
            na_keys = []
        if outsider_keys is None:
            outsider_keys = []
        if insider_keys is None:
            insider_keys = []
        if others is None:
            others = {}

        outsider_keys += ['県外', '府外', '都外'] + [k + '外' for k in insider_keys]
        na_keys += ['非公表', '調査中']

        series = self.dataframe[column].replace({
            **localization_dict,
            **{k: na_value for k in na_keys},
            **{k: outsider_value for k in outsider_keys},
            **{k: insider_value for k in insider_keys},
            **{k: outsider_value for k in localization.PREFECTURES.keys() if k not in insider_keys},
            **others,
        })
        series.fillna(na_value, inplace=True)
        if inplace:
            self.dataframe[column] = series

        return series

    def _localize(self, **kwargs):
        return self.dataframe

    def _cleanse(self, **kwargs):
        return self.dataframe

    def save_csv(self, save_path=None, index=False):
        if save_path is None:
            now = datetime.datetime.now()
            timestamp = now.strftime('%Y%m%d_%H%M')
            save_path = f'{timestamp}_{self.name}.csv'

        self.dataframe.to_csv(save_path, index=index)

    def to_dict(self, orient='record', replace_nan=False):
        data = self.dataframe.where(self.dataframe.notnull(), None) if replace_nan else self.dataframe
        return data.to_dict(orient=orient)

    def to_json(self):
        dict_data = self.to_dict(replace_nan=True)
        json_data = json.dumps(dict_data)
        return json_data

    def upload_to_storage(self, bucket, extension='json'):
        """Upload a Dataframe as JSON to Firebase Storage.

        returns
            storage_ref
        """
        storage_ref = f'{self.name}.{extension}'
        blob = bucket.blob(storage_ref)

        if extension == 'json':
            data_str = self.to_json()
        else:
            raise NotImplementedError(f'Unsupported file type "{extension}"')

        blob.upload_from_string(data_str, content_type='application/json')

        return storage_ref

    def upload_to_database(self, client, root, item_key=None, batch_size=FIREBASE_BATCH_SIZE):
        if item_key not in self.dataframe.columns:
            item_key = None
        data_dict = self.to_dict()
        for batched_data in batch_data(data_dict, batch_size):
            batch = client.batch()
            for data_item in batched_data:
                if item_key is not None:
                    doc_ref = client.collection(root).document(str(data_item[item_key]))
                else:
                    doc_ref = client.collection(root).document()
                batch.set(doc_ref, data_item)
            batch.commit()


class CsvDataset(Dataset):
    def __init__(self, url, name, **kwargs):
        super().__init__(url, name, **kwargs)

    def _create_dataframe(self):
        return pd.read_csv(self.url, **self.kwargs)


class ExcelDataset(Dataset):
    def __init__(self, url, name, sheet_id, header_row=0, **kwargs):
        super().__init__(url, name, **kwargs)
        self.sheet = sheet_id
        self.header_row = header_row

    def _create_dataframe(self):
        return pd.read_excel(self.url, self.sheet, header=self.header_row, **self.kwargs)


class JsonDataset(Dataset):
    def __init__(self, url, name, **kwargs):
        super().__init__(url, name, **kwargs)
        self.json = None

    def _get_json_from_url(self):
        request = urllib.request.Request(self.url, headers=QUERY_HEADERS)
        with urllib.request.urlopen(request) as url:
            data = json.loads(url.read().decode())
        return data

    def _create_dataframe(self):
        if self.json is None:
            self.json = self._get_json_from_url()
        return self._create_dataframe_from_json()

    def _create_dataframe_from_json(self):
        raise NotImplementedError()


class PdfDataset(Dataset):
    def __init__(self, url, name, pages='all', include_header=True, **kwargs):
        super().__init__(url, name, **kwargs)
        self.pages = pages
        self.include_header = include_header

    def _create_dataframe(self, **kwargs):
        if self.include_header:
            df = tabula.read_pdf(self.url, pages=self.pages, **kwargs)
        else:
            df = tabula.read_pdf(self.url, pages=self.pages, pandas_options={'header': None}, **kwargs)

        if isinstance(df, list):
            df = pd.concat(df)
        return df.reset_index()
