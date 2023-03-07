import re, random
import torch
import torch.nn as nn

import numpy as np
from transformers import AutoModelForQuestionAnswering, AutoTokenizer
from utils import correct_alignment


class BertQA:

    def __init__(self, config):
        self.batch_size = config['batch_size']
        self.model = AutoModelForQuestionAnswering.from_pretrained(config['model_weights'])
        self.tokenizer = AutoTokenizer.from_pretrained(config['model_weights'])
        self.page_retrieval = config['page_retrieval'].lower() if 'page_retrieval' in config else None
        self.ignore_index = 9999  # 0

    def parallelize(self):
        self.model = nn.DataParallel(self.model)

    def forward(self, batch, return_pred_answer=False):
        question = batch['questions']
        context = batch['contexts']
        answers = batch['answers']

        if self.page_retrieval == 'logits':
            outputs = []
            pred_answers = []
            pred_answer_pages = []
            for batch_idx in range(len(context)):
                document_encoding = self.tokenizer([question[batch_idx]]*len(context[batch_idx]), context[batch_idx], return_tensors="pt", padding=True, truncation=True)

                max_logits = -999999
                answer_page = None
                document_outputs = None
                for page_idx in range(len(document_encoding['input_ids'])):
                    input_ids = document_encoding["input_ids"][page_idx].to(self.model.device)
                    attention_mask = document_encoding["attention_mask"][page_idx].to(self.model.device)

                    # Retrieval with logits is available only during inference and hence, the start and end indices are not used.
                    # start_pos = torch.LongTensor(start_idxs).to(self.model.device) if start_idxs else None
                    # end_pos = torch.LongTensor(end_idxs).to(self.model.device) if end_idxs else None

                    page_outputs = self.model(input_ids.unsqueeze(dim=0), attention_mask=attention_mask.unsqueeze(dim=0))

                    start_logits_cnf = [page_outputs.start_logits[batch_ix, max_start_logits_idx.item()].item() for batch_ix, max_start_logits_idx in enumerate(page_outputs.start_logits.argmax(-1))][0]
                    end_logits_cnf = [page_outputs.end_logits[batch_ix, max_end_logits_idx.item()].item() for batch_ix, max_end_logits_idx in enumerate(page_outputs.end_logits.argmax(-1))][0]
                    page_logits = np.mean([start_logits_cnf, end_logits_cnf])

                    if page_logits > max_logits:
                        answer_page = page_idx
                        document_outputs = page_outputs
                        max_logits = page_logits

                outputs.append(None)  # outputs.append(document_outputs)  # During inference outputs are not used.
                pred_answers.append(self.get_answer_from_model_output([document_encoding["input_ids"][answer_page]], document_outputs)[0] if return_pred_answer else None)
                pred_answer_pages.append(answer_page)

        else:
            encoding = self.tokenizer(question, context, return_tensors="pt", padding=True, truncation=True)
            input_ids = encoding["input_ids"].to(self.model.device)
            attention_mask = encoding["attention_mask"].to(self.model.device)

            # start_pos = torch.LongTensor(start_idxs).to(self.model.device) if start_idxs else None
            # end_pos = torch.LongTensor(end_idxs).to(self.model.device) if end_idxs else None
            start_pos, end_pos, context_page_token_correspondent = self.get_start_end_idx(encoding, context, answers, batch['context_page_corresp'])

            outputs = self.model(input_ids, attention_mask=attention_mask, start_positions=start_pos, end_positions=end_pos)
            pred_answers, answ_confidence = self.get_answer_from_model_output(input_ids, outputs) if return_pred_answer else None

            if self.page_retrieval == 'oracle':
                pred_answer_pages = batch['answer_page_idx']

            elif self.page_retrieval == 'concat':
                pred_answer_pages = [context_page_token_correspondent[batch_idx][pred_start_idx] if len(context_page_token_correspondent[batch_idx]) > pred_start_idx else -1 for batch_idx, pred_start_idx in enumerate(outputs.start_logits.argmax(-1).tolist())]

            elif self.page_retrieval == 'none':
                pred_answer_pages = None

        return outputs, pred_answers, pred_answer_pages, answ_confidence

    def get_start_end_idx(self, encoding, context, answers, context_page_char_correspondent=None):

        pos_idx = []
        context_page_token_correspondent = []
        for batch_idx in range(len(context)):
            batch_pos_idxs = []
            for answer in answers[batch_idx]:
                start_idxs = [m.start() for m in re.finditer(re.escape(answer), context[batch_idx])]

                for start_idx in start_idxs:
                    end_idx = start_idx + len(answer)
                    start_idx, end_idx = correct_alignment(context[batch_idx], answer, start_idx, end_idx)

                    if start_idx is not None and end_idx != 0:
                        batch_pos_idxs.append([start_idx, end_idx])
                        break

            if len(batch_pos_idxs) > 0:
                start_idx, end_idx = random.choice(batch_pos_idxs)

                context_encodings = self.tokenizer.encode_plus(context[batch_idx], padding=True, truncation=True)
                start_positions_context = context_encodings.char_to_token(start_idx)
                end_positions_context = context_encodings.char_to_token(end_idx - 1)

                # here we will compute the start and end position of the answer in the whole example
                # as the example is encoded like this <s> question</s></s> context</s>
                # and we know the position of the answer in the context
                # we can just find out the index of the sep token and then add that to position + 1 (+1 because there are two sep tokens)
                # this will give us the position of the answer span in whole example
                sep_idx = encoding['input_ids'][batch_idx].tolist().index(self.tokenizer.sep_token_id)

                if start_positions_context is not None and end_positions_context is not None:
                    start_position = start_positions_context + sep_idx
                    end_position = end_positions_context + sep_idx

                else:
                    start_position, end_position = self.ignore_index, self.ignore_index

                pos_idx.append([start_position, end_position])

            else:
                pos_idx.append([self.ignore_index, self.ignore_index])

            # Page correspondence for concat:
            if self.page_retrieval == 'concat':
                context_encodings = self.tokenizer.encode_plus(context[batch_idx], padding=True, truncation=True)
                page_change_idxs = [0] + [i + 1 for i, x in enumerate(context_page_char_correspondent[batch_idx]) if x == -1]
                page_change_idxs_tokens = [context_encodings.char_to_token(idx) for idx in page_change_idxs]

                page_tok_corr = np.empty(len(context_encodings.input_ids))
                page_tok_corr.fill(-1)
                for page_idx in range(len(page_change_idxs_tokens)):
                    if page_change_idxs_tokens[page_idx] is None:
                        break

                    start_page_idx = page_change_idxs_tokens[page_idx]
                    if page_idx + 1 < len(page_change_idxs_tokens) and page_change_idxs_tokens[
                        page_idx + 1] is not None:
                        end_page_idx = page_change_idxs_tokens[page_idx + 1]
                    else:
                        end_page_idx = None

                    page_tok_corr[start_page_idx:end_page_idx] = page_idx

                context_page_token_correspondent.append(page_tok_corr)

        start_idxs = torch.LongTensor([idx[0] for idx in pos_idx]).to(self.model.device)
        end_idxs = torch.LongTensor([idx[1] for idx in pos_idx]).to(self.model.device)

        return start_idxs, end_idxs, context_page_token_correspondent

    def get_answer_from_model_output(self, input_tokens, outputs):
        start_idxs = torch.argmax(outputs.start_logits, axis=1)
        end_idxs = torch.argmax(outputs.end_logits, axis=1)

        answers = []
        answ_confidence = []
        for batch_idx in range(len(input_tokens)):
            context_tokens = self.tokenizer.convert_ids_to_tokens(input_tokens[batch_idx].tolist())

            answer_tokens = context_tokens[start_idxs[batch_idx]: end_idxs[batch_idx] + 1]
            answer = self.tokenizer.decode(
                self.tokenizer.convert_tokens_to_ids(answer_tokens)
            )

            answer = answer.strip()  # remove space prepending space token
            answers.append(answer)

            conf_mat = np.matmul(np.expand_dims(outputs.start_logits.softmax(dim=1)[batch_idx].unsqueeze(dim=0).cpu(), -1),
                                 np.expand_dims(outputs.end_logits.softmax(dim=1)[batch_idx].unsqueeze(dim=0).cpu(), 1)).squeeze(axis=0)

            answ_confidence.append(
                # (outputs.start_logits[batch_idx, start_idxs[batch_idx]].item() + outputs.end_logits[batch_idx, start_idxs[batch_idx]].item()) / 2
                # torch.matmul(outputs.start_logits[batch_idx], outputs.end_logits[batch_idx]).cpu()
                conf_mat[start_idxs[batch_idx], end_idxs[batch_idx]].item()
            )

        return answers, answ_confidence
