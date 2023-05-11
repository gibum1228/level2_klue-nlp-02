import pickle
import torch
import pandas as pd
import pytorch_lightning as pl

from sklearn.model_selection import train_test_split
from tqdm.auto import tqdm
from torch.utils.data import Dataset, DataLoader

from hangulize import hangulize
import re

class Dataset(Dataset):
    """
    Dataloader에서 불러온 데이터를 Dataset으로 만들기
    """

    def __init__(self, inputs, targets=[]):
        self.inputs = inputs
        self.targets = targets

    def __getitem__(self, idx):
        inputs = {key: val[idx].clone().detach()
                  for key, val in self.inputs.items()}

        if self.targets:
            targets = torch.tensor(self.targets[idx])

            return inputs, targets
        else:
            return inputs

    def __len__(self):
        return len(self.inputs['input_ids'])


class Dataloader(pl.LightningDataModule):
    """
    원본 데이터를 불러와 전처리 후 Dataloader 만들어 Dataset에 넘겨 최종적으로 사용할 데이터셋 만들기
    """

    def __init__(self, tokenizer, CFG):
        super(Dataloader, self).__init__()
        self.CFG = CFG
        self.tokenizer = tokenizer
        
        train_x, train_y, predict_x = load_data()
        train_x, val_x, train_y, val_y = train_test_split(train_x, train_y,
                                                          stratify=train_y,
                                                          test_size=self.CFG['train']['test_size'],
                                                          shuffle=self.CFG['train']['shuffle'],
                                                          random_state=self.CFG['seed'])
        self.train_x = train_x
        self.train_y = train_y
        self.val_x = val_x
        self.val_y = val_y
        self.predict_x = predict_x
        self.label2num = load_label2num()

        self.train_dataset = None
        self.val_dataset = None  # val == test
        self.test_dataset = None
        self.predict_dataset = None

    def tokenizing(self, x):
        """ 토크나이징 함수
        
        Note:   두 entity를 [SEP]토큰으로 이어붙인 리스트와 
                원래 x['sentence'] 리스트를 토크나이저에 인자로 집어넣습니다.
                inputs는 따라서 input_ids, attention_mask, token_type_ids가 각각 포함된 배열형태로 구성됩니다.
                
        Arguments:
        x: pd.DataFrame
        
        Returns:
        inputs: Dict({'input_ids', 'token_type_ids', 'attention_mask'}), 각 tensor(num_data, max_length)
        """
        concat_entity = []
        for sub_ent, obj_ent in zip(x['subject_entity'], x['object_entity']):
            concat_entity.append(obj_ent + " [SEP] " + sub_ent)

        inputs = self.tokenizer(
            concat_entity,
            list(x['sentence']),
            return_tensors='pt',
            padding=True,
            truncation=True,
            max_length=self.CFG['train']['token_max_len'],
            add_special_tokens=True,
        )

        return inputs

    def preprocessing(self, x, y=[], train=False):
        DC = DataCleaning(self.CFG['select_DC'])
        DA = DataAugmentation(self.CFG['select_DA'])

        x = DC.process(x)
        # 데이터 증강
        if train:
            x = DA.process(x)

        # 텍스트 데이터 토큰화
        inputs = self.tokenizing(x)
        targets = [self.label2num[label] for label in y]
        
        return inputs, targets

    def setup(self, stage='fit'):
        if stage == 'fit':
            # 학습 데이터 준비
            train_inputs, train_targets = self.preprocessing(
                self.train_x, self.train_y, train=True)
            self.train_dataset = Dataset(train_inputs, train_targets)
            
            val_inputs, val_targets = self.preprocessing(self.val_x, self.val_y)
            self.val_dataset = Dataset(val_inputs, val_targets)
        else:
            # 평가 데이터 호출
            predict_inputs, predict_targets = self.preprocessing(
                self.predict_x)
            self.predict_dataset = Dataset(predict_inputs, predict_targets)

    def train_dataloader(self):
        return DataLoader(self.train_dataset, batch_size=self.CFG['train']['batch_size'], shuffle=self.CFG['train']['shuffle'])
    
    def val_dataloader(self):
        return DataLoader(self.val_dataset, batch_size=self.CFG['train']['batch_size'])

    def predict_dataloader(self):
        return DataLoader(self.predict_dataset, batch_size=self.CFG['train']['batch_size'])


class DataCleaning():
    """
    config select DC에 명시된 Data Cleaning 기법을 적용시켜주는 클래스
    """
    def __init__(self, select_list):
        self.select_list = select_list

    def process(self, df):
        if self.select_list:
            for method_name in self.select_list:
                method = eval("self." + method_name)
                df = method(df)

        return df

    """
    data cleaning 코드
    """

    def entity_parsing(self, df):
        """ entity에서 word, start_idx, end_idx, type 분리하기
        Note: <데이터 예시>
            subject_entity : {'word': '비틀즈', 'start_idx': 24, 'end_idx': 26, 'type': 'ORG'}
            object_entity : {'word': '조지 해리슨', 'start_idx': 13, 'end_idx': 18, 'type': 'PER'}
            
        Arguments:
        df: Cleaning을 수행하고자 하는 DataFrame
        
        Return:
        df: Cleaning 작업이 완료된 DataFrame
        """
        for type_entity in ['subject', 'object']:
            column = f"{type_entity}_entity"

            word_list, type_list = [], []
            start_idx_list, end_idx_list = [], []

            for i in range(len(df)):
                dictionary = eval(df.iloc[i][column])

                word_list.append(dictionary['word'])
                start_idx_list.append(dictionary['start_idx'])
                end_idx_list.append(dictionary['end_idx'])
                type_list.append(dictionary['type'])

            df[column] = word_list
            for key in ['start_idx', 'end_idx', 'type']:
                df[f"{type_entity}_{key}"] = eval(f"{key}_list")
        
        return df
    
    def pronounce_japan(self, df):
        """ sentence, subject_entity, object_entity 컬럼에서 일본어 발음을 한글로 변환
        hangulize 라이브러리 사용
        
        Note: <데이터 예시>
        みなみ → 미나미
        
        Arguments:
        df: pronounce_japan을 수행하고자 하는 DataFrame
        
        Return:
        df: pronounce_japan 작업이 완료된 DataFrame
        """
        pattern = re.compile(r'[ぁ-ゔ]+|[ァ-ヴー]+[々〆〤]')
        language = 'jpn'
        for i in range(len(df)):
            if pattern.search(df.loc[i,'sentence']):
                df.loc[i, 'sentence'] = hangulize(df.loc[i, 'sentence'], language)
            if pattern.search(df.loc[i,'subject_entity']):
                df.loc[i, 'subject_entity'] = hangulize(df.loc[i, 'subject_entity'], language)
            if pattern.search(df.loc[i,'object_entity']):
                df.loc[i, 'object_entity'] = hangulize(df.loc[i, 'object_entity'], language)
        return df

    def entity_mask(self, df):
        """ sentence 컬럼에서 subject_entity와 object_entity에 마스킹 처리(index 기준으로)
        subject_entity → [SUB] 마스킹
        object_entity → [OB] 마스킹
        
        Note: <데이터 예시>
        〈Something〉는 조지 해리슨이 쓰고 비틀즈가 1969년 앨범 《Abbey Road》에 담은 노래다.
            ↓
        〈Something〉는 [OB]이 쓰고 [SUB]가 1969년 앨범 《Abbey Road》에 담은 노래다.
        
        Arguments:
        df: entity_mask를 수행하고자 하는 DataFrame
        
        Return:
        df: entity_mask 작업이 완료된 DataFrame
        """
        for idx, row in df.iterrows():
            if row['subject_start_idx']<row['object_start_idx']:
                df.loc[idx,'sentence']=row['sentence'][:row['subject_start_idx']]+'[SUB]'+row['sentence'][row['subject_end_idx']+1:row['object_start_idx']]+'[OB]'+row['sentence'][row['object_end_idx']+1:]
            else:
                df.loc[idx,'sentence']=row['sentence'][:row['object_start_idx']]+'[OB]'+row['sentence'][row['object_end_idx']+1:row['subject_start_idx']]+'[SUB]'+row['sentence'][row['subject_end_idx']+1:]
        return df


class DataAugmentation():
    """
    config select DA에 명시된 Data Augmentation 기법을 적용시켜주는 클래스
    """

    def __init__(self, select_list):
        self.select_list = select_list

    def process(self, df):
        if self.select_list:
            aug_df = pd.DataFrame(columns=df.columns)

            for method_name in self.select_list:
                method = eval("self." + method_name)
                aug_df = pd.concat([aug_df, method(df)])

            df = pd.concat([df, aug_df])

        return df

    """
    data augmentation 코드
    """


def load_data():
    """
    학습 데이터와 테스트 데이터 DataFrame 가져오기
    """
    train_df = pd.read_csv('./dataset/new_train.csv')
    train_df.drop(['id', 'source'], axis=1, inplace=True)
    train_x = train_df.drop(['label'], axis=1)
    train_y = train_df['label']
    test_x = pd.read_csv('./dataset/new_test.csv')
    test_x.drop(['id', 'source'], axis=1, inplace=True)
    
    return train_x, train_y, test_x


def load_label2num():
    with open('./code/dict_label_to_num.pkl', 'rb') as f:
        label2num = pickle.load(f)
    
    return label2num


def load_num2label():
    with open('./code/dict_num_to_label.pkl', 'rb') as f:
        num2label = pickle.load(f)
    
    return num2label


if __name__ == "__main__":
    train_df = pd.read_csv('./dataset/train/train.csv')
    test_df = pd.read_csv('./dataset/test/test_data.csv')

    # entity_parsing이 적용된 DataFrame 파일 만들기
    new_train_df = DataCleaning([]).entity_parsing(train_df.copy(deep=True))
    new_test_df = DataCleaning([]).entity_parsing(test_df.copy(deep=True))

    new_train_df.to_csv('./dataset/new_train.csv', index=False)
    new_test_df.to_csv('./dataset/new_test.csv', index=False)